# SPDX-License-Identifier: MIT
"""The iOS Codable structs, mirrored in Python, run against the real responses.

WHY THIS FILE EXISTS. Four times in one month a release shipped a payload the
app could not decode, and every one of them was found by a person holding a
phone rather than by this suite:

  * v0.17.1 — the login QR had no `type`, so `LoginQRParser` refused every scan.
  * v0.17.2 — Tailscale stripped `Authorization` and every call arrived as Guest.
  * v0.18.2 — `claim_task` answered `{"name": null, …}` because
    `dispatch.claim_farm_task` spreads the task at the TOP LEVEL of `data` and
    the wrapper asked for a `"task"` key that was not there. `FarmTask` decodes
    `name` with `try c.decode(String.self)`, so the row did not degrade — it
    threw, mid-flow, on a claim the worker had already made.
  * v0.18.4 — evidence Files came back unreadable to anyone but the uploader.

THE COMMON SHAPE IS THE POINT. The server was tested against itself and passed
every time. What nobody had written down in code was the *other* side's reading
of the same bytes, so the two drifted apart silently and the drift was published.

WHAT A MIRROR IS. Each `Codable` subclass below is a transcription of one struct
in `fafo_ios/FarmOpsKit/Sources/FarmOpsKit/Models/`, and it records the one
distinction that decides whether a bad payload is a blank line on a screen or a
crash in a worker's hands:

    STRICT   `let x: String` decoded with `try c.decode(...)`. Absent, null or
             the wrong type and the WHOLE ROW throws. This is the v0.18.2 class.
    LENIENT  decoded through `c.lenientString(...)` and friends, which return
             nil on absence OR mismatch. A miss here is silent — the field just
             is not on the screen — which is why they are checked too. An
             inspection whose "Why this task exists" card is quietly missing
             looks like an inspection nobody could justify.
    NULLABLE `let x: String?` on a struct with a synthesized `init(from:)` and
             no lenient helper. Absence is fine, the WRONG TYPE throws. Between
             the two, and it is what `ExistingEmployee` and `CreatedEmployee`
             actually do — calling either of them LENIENT would claim a
             tolerance the app does not have.

Each mirror carries the Swift file and line the rule is transcribed from, and a
failure quotes it, so somebody reading a red test is one click from the source of
truth rather than from this transcription of it.

THE PAYLOAD IS PUT THROUGH JSON FIRST, deliberately. `frappe.as_json` is what
turns a `datetime` into `"2026-08-02 21:14:03"` on the way to the phone, and
half the drift this file is looking for lives in that conversion. Asserting
against the Python dict the wrapper returned would test one step short of the
thing that actually broke.

WHAT THIS FILE DOES NOT PROVE. That the app compiles, or that Swift's decoder
behaves exactly as transcribed here. `test_api_mobile.TheAppCanDecodeThis` makes
the same bet and states it: a transcription is only worth having if it is strict
enough to have caught the bug it was written for, so
`TheMirrorsAreStrictEnough` below feeds each mirror the exact broken payload
from the release that shipped it and asserts the mirror refuses it.
"""

import base64
import io
import json
import struct
import unittest
import zlib
from typing import ClassVar

import frappe

from erpnext_mcp import compliance_rules, i9_pdf, w4_pdf
from erpnext_mcp.api import files as files_api
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import badges as badges_tool
from erpnext_mcp.tools import i9 as i9_tools

from .fixtures import MAIN, MAIN_ABBR, install_hrms
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, MobileAPITestCase
from .test_api_mobile import TheSurfaceIsClosed as _MobileSurface

#: Frappe's own timestamp renderings, from `FrappeDate.formatters` +
#: `ISO8601DateFormatter` in `LenientDecoding.swift:70-85`. A date the app cannot
#: parse decodes to nil, which puts "—" where a claim time should be.
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S")

#: The smallest thing that is genuinely a PNG. `signatures._sniff` reads the
#: first eight bytes and refuses anything else, and nothing here asks a renderer
#: to open the result — so a real image would be bytes spent proving something
#: no test in this file asserts. Same constant, same reasoning, as
#: `test_missing_signatures.A_CAPTURE`.
A_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"signature").decode()


def _a_real_png(size: int = 64) -> bytes:
	"""A PNG a renderer can actually open, for the one test that needs one.

	`A_PNG` above is eight magic bytes and is right for every test that only
	asks whether the signature was STORED. It is exactly wrong for the test that
	asks whether the signature reached the PAGE: `i9.render_i9_pdf` swallows a
	capture it cannot read — deliberately, because losing one image should not
	stop an employer printing the form — so a fixture that merely looks like a
	PNG takes the same silent path as the bug, and the test passes while the
	page comes back blank.
	"""

	def chunk(kind: bytes, payload: bytes) -> bytes:
		body = kind + payload
		return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

	scanlines = b"".join(
		bytes([0]) + bytes([0 if ((x // 8 + y // 8) % 2 == 0) else 255 for x in range(size)])
		for y in range(size)
	)
	return (
		b"\x89PNG\r\n\x1a\n"
		+ chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0))
		+ chunk(b"IDAT", zlib.compress(scanlines))
		+ chunk(b"IEND", b"")
	)


A_REAL_PNG = _a_real_png()


class ContractError(AssertionError):
	"""A drift between what the server emitted and what the app decodes."""


# ── the mirror machinery ────────────────────────────────────────────────────
class Codable:
	"""One iOS `Codable` struct, transcribed.

	Subclasses declare their fields and nothing else; `decode` is the whole
	implementation, and it is deliberately dumb — every rule it applies is one
	line of Swift it can point at.
	"""

	#: The file this mirror transcribes, for the failure message.
	SWIFT = ""
	#: `(json_key, python_type, swift_line)` — `try c.decode`, throws on a miss.
	STRICT = ()
	#: `(json_key, python_type, swift_line)` — `c.lenient*`, nil on a miss.
	LENIENT = ()
	#: `(json_key, python_type, swift_line)` — a `String?`/`Int?` on a struct with
	#: SYNTHESIZED `init(from:)` and no lenient helper. Absent or null is fine and
	#: the field is simply nil; the WRONG TYPE throws exactly as a strict field
	#: does, because Swift's generated decoder calls `decodeIfPresent`, which is
	#: forgiving about presence and not about type. Between STRICT and LENIENT, and
	#: neither of them would be a true transcription of `ExistingEmployee`.
	NULLABLE = ()
	#: `(json_key, swift_line)` — `c.lenientDate`, nil on an unparseable string.
	DATES = ()
	#: `(json_key, allowed_values, swift_line)` — a Swift enum with an `unknown`
	#: case. Decoding never fails; an unrecognised value degrades, which is a
	#: silent miss and therefore checked.
	ENUMS = ()
	#: `(json_key, Codable subclass, is_list, swift_line)`.
	NESTED = ()

	@classmethod
	def decode(cls, payload, method, path="") -> dict:
		where = f"{method}{path}"
		if not isinstance(payload, dict):
			raise ContractError(
				f"{where} answered {type(payload).__name__} where {cls.__name__} "
				f"({cls.SWIFT}) decodes an object."
			)

		for key, wanted, line in cls.STRICT:
			cls._strict(payload, key, wanted, where, line)
		for key, wanted, line in cls.LENIENT:
			cls._lenient(payload, key, wanted, where, line)
		for key, wanted, line in cls.NULLABLE:
			cls._nullable(payload, key, wanted, where, line)
		for key, line in cls.DATES:
			cls._date(payload, key, where, line)
		for key, allowed, line in cls.ENUMS:
			cls._enum(payload, key, allowed, where, line)
		for key, model, is_list, line in cls.NESTED:
			cls._nested(payload, key, model, is_list, method, path, line)
		return payload

	# ── one rule per method, each naming its Swift line ─────────────────────
	@classmethod
	def _strict(cls, payload, key, wanted, where, line) -> None:
		if key not in payload:
			raise ContractError(
				f"{where} did not emit {key!r}. {cls.SWIFT}:{line} decodes it with "
				f"`try c.decode(...)`, so the whole row throws — this is not a blank "
				f"field on a screen, it is a crash in a worker's hand. "
				f"Emitted: {sorted(payload)}"
			)
		value = payload[key]
		if value is None:
			raise ContractError(
				f"{where} emitted {key!r} as null. {cls.SWIFT}:{line} decodes it with "
				f"`try c.decode(...)`, which throws on null — THIS IS THE v0.18.2 BUG, "
				f"in which claim_task answered {{'name': null}} and iOS reported "
				f"\"Bad value at 'name'\"."
			)
		if not isinstance(value, wanted):
			raise ContractError(
				f"{where} emitted {key!r} as {type(value).__name__} ({value!r}). "
				f"{cls.SWIFT}:{line} decodes {wanted.__name__}, and a strict decode of "
				f"the wrong type throws exactly as null does."
			)

	@classmethod
	def _lenient(cls, payload, key, wanted, where, line) -> None:
		value = payload.get(key)
		if value is None:
			# Genuinely absent is allowed — that is what lenient MEANS. The app
			# renders a default or hides the row's card.
			return
		if not isinstance(value, wanted):
			raise ContractError(
				f"{where} emitted {key!r} as {type(value).__name__} ({value!r}), and "
				f"{cls.SWIFT}:{line} reads it with a lenient {wanted.__name__} decode "
				f"that returns nil on a type mismatch. Nothing crashes — the field just "
				f"silently is not there, which is why this is asserted rather than "
				f"trusted."
			)

	@classmethod
	def _nullable(cls, payload, key, wanted, where, line) -> None:
		value = payload.get(key)
		if value is None:
			# `decodeIfPresent` on an absent or null key is nil, and the struct
			# declares the property optional. Nothing to answer for.
			return
		if not isinstance(value, wanted):
			raise ContractError(
				f"{where} emitted {key!r} as {type(value).__name__} ({value!r}). "
				f"{cls.SWIFT}:{line} declares {wanted.__name__}? on a struct with a synthesized "
				f"decoder, so a wrong TYPE throws the whole row even though a missing key would "
				f"not — this crashes in a worker's hand exactly as a strict field does."
			)

	@classmethod
	def _date(cls, payload, key, where, line) -> None:
		value = payload.get(key)
		if value is None or value == "":
			return
		import datetime

		if isinstance(value, (datetime.datetime, datetime.date)):
			raise ContractError(
				f"{where} emitted {key!r} as a Python {type(value).__name__} that survived "
				f"JSON encoding. It reaches the phone as whatever `frappe.as_json` made of "
				f"it, and {cls.SWIFT}:{line} parses a string."
			)
		if not isinstance(value, str):
			raise ContractError(
				f"{where} emitted {key!r} as {type(value).__name__}; {cls.SWIFT}:{line} "
				f"reads it with `c.lenientDate`, which decodes a String and nothing else."
			)
		for fmt in _DATE_FORMATS:
			try:
				datetime.datetime.strptime(value, fmt)
				return
			except ValueError:
				continue
		raise ContractError(
			f"{where} emitted {key!r} as {value!r}, which none of `FrappeDate`'s formats "
			f"parse ({', '.join(_DATE_FORMATS)}). {cls.SWIFT}:{line} decodes it to nil, so "
			f"the app shows no time at all rather than the wrong one."
		)

	@classmethod
	def _enum(cls, payload, key, allowed, where, line) -> None:
		value = payload.get(key)
		if value is None:
			return
		if value not in allowed:
			raise ContractError(
				f"{where} emitted {key}={value!r}, which is not a case of the Swift enum at "
				f"{cls.SWIFT}:{line} ({', '.join(sorted(allowed))}). It decodes to `.unknown`, "
				f'which the app renders as "Unrecognized" — the state machine has moved and '
				f"the fielded build has not."
			)

	@classmethod
	def _nested(cls, payload, key, model, is_list, method, path, line) -> None:
		value = payload.get(key)
		if value is None:
			return
		if is_list:
			if not isinstance(value, list):
				raise ContractError(
					f"{method}{path} emitted {key!r} as {type(value).__name__}; "
					f"{cls.SWIFT}:{line} decodes an array."
				)
			for index, entry in enumerate(value):
				model.decode(entry, method, f"{path}.{key}[{index}]")
			return
		model.decode(value, method, f"{path}.{key}")


# ── the mirrors ─────────────────────────────────────────────────────────────
class EvidenceContractModel(Codable):
	"""`EvidenceContract.swift` — every field lenient, defaults on a miss."""

	SWIFT = "EvidenceContract.swift"
	LENIENT = (
		("photos", bool, 42),
		("signature", bool, 43),
		("findings_text", bool, 44),
		("witness", bool, 45),
		("min_photos", int, 46),
	)


class FarmTaskModel(Codable):
	"""`FarmTask.swift` — `name` is the only strict field, and it is the one that
	broke. Everything else degrades to a default or an empty card."""

	SWIFT = "FarmTask.swift"
	STRICT = (("name", str, 105),)
	LENIENT = (
		("task_name", str, 107),
		("task_type", str, 108),
		("estimated_duration_minutes", int, 112),
		("skill_required", str, 113),
		("notes", str, 114),
		("company", str, 115),
		("location", str, 116),
		("location_type", str, 117),
		("latitude", float, 118),
		("longitude", float, 119),
		("creates_record", str, 121),
		("source_alert", str, 122),
		("source_alert_explanation", str, 123),
		("assignment", str, 124),
		("assigned_to", str, 125),
	)
	DATES = (("claimed_at", 126), ("started_at", 127))
	ENUMS = (
		(
			"state",
			{
				"Draft",
				"Available",
				"Claimed",
				"In-Progress",
				"Awaiting-Review",
				"Completed",
				"Rejected",
				"Cancelled",
			},
			109,
		),
		("urgency", {"Low", "Normal", "High", "Critical"}, 110),
		("dispatch_mode", {"Dispatched", "Self-pick", "Either"}, 111),
	)
	NESTED = (("evidence_required", EvidenceContractModel, False, 120),)


class AccessibleCompanyModel(Codable):
	"""`UserContext.swift` — a company whose `name` is null takes the whole
	context down with it, and the context is the first call after a scan."""

	SWIFT = "UserContext.swift"
	STRICT = (("name", str, 17),)
	LENIENT = (("abbr", str, 18),)


class UserContextModel(Codable):
	SWIFT = "UserContext.swift"
	STRICT = (("user", str, 56),)
	LENIENT = (("full_name", str, 57), ("employee", str, 58), ("default_company", str, 61))
	NESTED = (("companies", AccessibleCompanyModel, True, 60),)


class SignatureRequestModel(Codable):
	"""`SignatureRequest.swift` — the address a pad is opened at.

	EVERY FIELD IS LENIENT AND THE FIRST THREE STILL DECIDE EVERYTHING. Nothing
	here throws; what a miss costs is `isActionable`, and an unactionable request
	means the row falls through to the task or to its own detail rather than
	opening a pad addressed at nothing. That failure is silent on the phone,
	which is exactly why it is asserted here: an alert that quietly stopped being
	tappable looks like an alert nobody bothered to resolve.
	"""

	SWIFT = "SignatureRequest.swift"
	LENIENT = (
		("doctype", str, 181),
		("docname", str, 182),
		("signature_field", str, 183),
		("signer_role", str, 186),
		("signer_label", str, 187),
		("employee", str, 188),
		("employee_name", str, 189),
		("form_label", str, 190),
		("section_label", str, 191),
		("attestation", str, 192),
		("printed_name", str, 193),
	)
	DATES = (("due_date", 194),)


class ComplianceAlertSummaryModel(Codable):
	SWIFT = "ComplianceAlertSummary.swift"
	STRICT = (("name", str, 31),)
	LENIENT = (
		("title", str, 32),
		("explanation", str, 33),
		("company", str, 36),
		("regulation", str, 37),
		("linked_task", str, 38),
		# v0.57.0 — §8.1. Displayed rather than navigated: this app has no screen
		# for an arbitrary doctype and does not pretend to have one.
		("subject_doctype", str, 89),
		("subject_docname", str, 90),
		# `?? false` on the Swift side, so an absent key is a v1 calendar row with
		# no Dismiss button — which is the safe direction for a permission to
		# fail, and the reason a site that never sends it loses nothing.
		("can_dismiss", bool, 91),
	)
	DATES = (("due_date", 34),)
	ENUMS = (("urgency", {"Low", "Normal", "High", "Critical"}, 35),)
	NESTED = (("signature_request", SignatureRequestModel, False, 86),)


class SignedPDFModel(Codable):
	"""`SignatureAPI.SignedPDF` — the completed form, as a page the phone opens.

	THE BYTES ARE IN THE ANSWER BECAUSE `file_url` IS UNREACHABLE HERE. It names
	a private File on the site and this app authenticates to the sidecar with
	`X-FarmOps-Token` rather than to Frappe, so the URL is a login page to the
	handset — the same reason `get_employee_badge_pass` sends its `.pkpass`
	down. §14.3 has said `file_url` is "recorded, not displayed" since it was
	written; this is what makes the page displayable.

	`available` IS WHAT THE SCREEN BRANCHES ON and it is false for three
	different reasons — no `pypdf`, no rendered page, a page that read back
	empty — each with its sentence in `note`. NONE of them says anything about
	whether the signature landed, which is settled by the call returning.
	"""

	SWIFT = "SignatureAPI.swift"
	LENIENT = (
		("available", bool, 46),
		("regenerated", bool, 49),
		("file_url", str, 50),
		("file_name", str, 51),
		("bytes", int, 52),
		("note", str, 54),
		("base64", str, 58),
	)


class SignatureSubmissionModel(Codable):
	"""`SignatureAPI.SubmissionResult` — what comes back from the pad.

	EVERY KEY IS OPTIONAL TO THE CLIENT and a sparse `{}` still reads as "signed,
	task closed", because that is the route's contract. What the app does with
	these is report: the form's new status, whether the task closed, which
	calendar row to take away, and — since v0.57.1 — the page that was signed.
	"""

	SWIFT = "SignatureAPI.swift"
	LENIENT = (
		("doctype", str, 72),
		("docname", str, 73),
		("field", str, 74),
		("form_status", str, 75),
		("file_url", str, 76),
		("task", str, 77),
		("task_state", str, 78),
		("task_completed", bool, 79),
		("already_signed", bool, 81),
		("dismissed_alert", str, 82),
	)
	DATES = (("signed_on", 80),)
	NESTED = ((("pdf"), SignedPDFModel, False, 83),)


class CompletionResultModel(Codable):
	"""`CompletionSubmission.swift` — every field lenient, which is the reason
	v0.18.2's completion drift showed up as a worker being told "Done" instead of
	"Housing Inspection HI-… created" rather than as a crash."""

	SWIFT = "CompletionSubmission.swift"
	LENIENT = (
		("task", str, 170),
		("created_record_doctype", str, 171),
		("created_record_name", str, 172),
		("dismissed_alert", str, 173),
		("corrective_action_opened", bool, 174),
	)


class RejectionModel(Codable):
	"""`FarmOpsAPI.reject` discards the body, so nothing here is strict at the
	decoder. `task` is asserted anyway: it is the only proof in the response that
	the rejection landed on the task the worker named, and a wrapper that stopped
	emitting it would be invisible until an audit."""

	SWIFT = "FarmOpsAPI.swift"
	STRICT = (("task", str, 103),)
	LENIENT = (("returned_to_state", str, 103), ("reason", str, 103))


class StagedChunkModel(Codable):
	"""`ChunkUploader.swift:63` calls this through `callVoid` — the app reads
	nothing back. The progress fields are asserted regardless, because the
	sequential uploader's ONLY recovery path after a timeout is re-sending the
	index the server says it wants next."""

	SWIFT = "ChunkUploader.swift"
	LENIENT = (
		("upload_id", str, 63),
		("file_name", str, 65),
		("chunk_index", int, 66),
		("chunk_count", int, 67),
		("chunks_received", int, 63),
		("next_expected_index", int, 63),
		("complete", bool, 63),
	)


class FinalizedFileModel(Codable):
	"""`ChunkUploader.FinalizeResponse` — the one place in the app that throws on
	a MISSING pair rather than a missing field. `file_token` or `file_url` must
	survive; neither and the completion cannot be submitted at all."""

	SWIFT = "ChunkUploader.swift"
	LENIENT = (("file_token", str, 33), ("file_url", str, 40), ("file_name", str, 33))

	@classmethod
	def decode(cls, payload, method, path=""):
		payload = super().decode(payload, method, path)
		if not (payload.get("file_token") or payload.get("file_url")):
			raise ContractError(
				f"{method} returned neither `file_token` nor `file_url`. "
				f'{cls.SWIFT}:39 throws `FrappeError.decoding("Upload finalize returned no '
				f'file handle.")` on exactly this, and the evidence a worker has already '
				f"captured cannot be attached to anything."
			)
		return payload


# ── v0.24.0: Universal Asset Tags ──────────────────────────────────────────
class ScanAssetModel(Codable):
	"""`AssetScanResponse` — what the phone shows after scanning a tag."""

	SWIFT = "AssetScanResponse.swift"
	STRICT = (("name", str, 5), ("scan_recorded", bool, 6))
	LENIENT = (
		("asset_type", str, 8),
		("company", str, 9),
		("description", str, 12),
	)


class AssetDetailModel(Codable):
	"""`AssetDetailResponse` — the full detail screen behind an asset tag."""

	SWIFT = "AssetDetailResponse.swift"
	STRICT = (("name", str, 5),)
	LENIENT = (
		("asset_type", str, 8),
		("company", str, 9),
		("description", str, 12),
	)


class StateChangeModel(Codable):
	"""`StateChangeResponse` — confirmation after logging a state change."""

	SWIFT = "StateChangeResponse.swift"
	STRICT = (
		("asset_name", str, 5),
		("action", str, 6),
		("from_state", str, 7),
		("to_state", str, 8),
	)
	LENIENT = (
		("asset_type", str, 10),
		("log_name", str, 11),
		("performed_by", str, 12),
	)


class AvailableActionsModel(Codable):
	"""`AvailableActionsResponse` — what a worker can do to an asset now."""

	SWIFT = "AvailableActionsResponse.swift"
	STRICT = (
		("asset_name", str, 5),
		("current_state", str, 6),
	)
	LENIENT = (
		("asset_type", str, 8),
		("available_actions", list, 9),
	)


class CreatedEmployeeModel(Codable):
	"""`OnboardingAPI.CreatedEmployee` — what step 1 of the wizard keeps.

	THE ONE STRICT FIELD IS THE ONE EVERY LATER STEP IS ADDRESSED TO. `employeeName`
	becomes `employee` on `create_i9_form`, on both I-9 sections, on the W-4 and on
	the badge map; a null or absent `name` here throws on the person's first
	morning, after their date of birth has been typed in and before anything has
	been filed. This is the v0.18.2 class of bug with four more screens behind it.

	`employee_id` is `String?` — a site that does not keep payroll numbers sends
	null and the app falls back to the docname (`OnboardingWizardViewModel:181`).
	NULLABLE rather than LENIENT because the struct has no lenient decoding: an
	`employee_id` that arrived as a NUMBER would throw.
	"""

	SWIFT = "OnboardingAPI.swift"
	STRICT = (("name", str, 16),)
	NULLABLE = (("employee_id", str, 17),)


class ExistingEmployeeModel(Codable):
	"""`ExistingEmployee` — one row of the search the Identity step opens with.

	`name` and `employee_name` are non-optional `String`s on a synthesized decoder,
	so either one missing throws the WHOLE LIST and the foreman sees an error where
	the returning picker's name should be — and then hires them a second time,
	which is the duplicate `create_employee` exists to refuse.

	`status` is what the wizard branches on (`OnboardingWizardViewModel:171`):
	anything other than "Active" routes to `reactivate_employee`. It is a plain
	optional String rather than a Swift enum, so an unexpected value does not
	degrade to `.unknown` — it reactivates.
	"""

	SWIFT = "OnboardingModels.swift"
	STRICT = (("name", str, 274), ("employee_name", str, 275))
	NULLABLE = (
		("employee_id", str, 276),
		("status", str, 277),
		("date_of_joining", str, 278),
		("employment_type", str, 279),
	)


class EmployeeDetailModel(Codable):
	"""`EmployeeDetail` — the record the returning-worker path skips steps on.

	`name` and `employee_name` are non-optional `String`s on a synthesized decoder,
	so either one missing throws and the foreman is left on the Identity step with
	a person standing in front of them who demonstrably exists — they were just
	picked out of `search_employees`.

	THE THREE STATUS FIELDS ARE WHY THIS MIRROR IS STRICTER THAN IT LOOKS.
	`satisfiedSteps` (`OnboardingModels.swift:352`) reads `i9_status`, `w4_status`
	and `badge_id` and marks whole wizard steps done from them. A wrong TYPE
	throws the row like any other; a wrong VALUE does not throw at all, it just
	silently re-collects an I-9 — or, far worse in the other direction, skips one.
	They are plain optional Strings rather than Swift enums, so there is no
	`.unknown` case to degrade into and nothing on the handset would report it.
	The value side is asserted in `test_30`, against the site's own Select options.

	`jurisdiction` is NOT on `ExistingEmployee` and is here: it arrived with the
	same iOS change that moved this call onto the sidecar, and the W-4 step needs
	it the moment a crew works across a state line.
	"""

	SWIFT = "OnboardingModels.swift"
	STRICT = (("name", str, 294), ("employee_name", str, 295))
	NULLABLE = (
		("first_name", str, 296),
		("last_name", str, 297),
		("employee_id", str, 298),
		("status", str, 299),
		("gender", str, 300),
		("date_of_birth", str, 301),
		("date_of_joining", str, 302),
		("company", str, 303),
		("i9_status", str, 304),
		("w4_status", str, 305),
		("badge_id", str, 306),
		("jurisdiction", str, 307),
	)


class VoidResponseModel(Codable):
	"""The onboarding methods whose answer the app decodes NOTHING out of.

	`OnboardingAPI` calls the five through `FrappeClient.callVoid`, which returns
	the raw `Data` and throws it away — the flow advances on the HTTP status and on
	nothing else. `reactivateEmployee` discards its answer the same way. So there
	is no struct to transcribe, and a mirror that invented one would be asserting a
	contract neither side has.

	WHAT IS STILL WORTH ASSERTING is that the answer is a JSON OBJECT. `callRaw`
	unwraps Frappe's `message` envelope for every other call in the app, and a
	wrapper that answered a bare list or a string would be the shape no later
	decoder could be added to without a server change — which is exactly the drift
	this file exists to catch early. The onboarding screens will grow a decoder the
	first time one of them wants to show the I-9's docname back to the person who
	filled it in.
	"""

	SWIFT = "OnboardingAPI.swift"


class AttachedDocumentModel(Codable):
	"""`OnboardingAPI.AttachedDocument` — v0.48.3, and the reason it is a struct.

	Every OTHER onboarding call the wizard makes goes through `callVoid` and the
	flow advances on the HTTP status alone (`VoidResponseModel` says why). This
	one does not, and must not: the whole defect this release closes is a call
	that answered 200 and stored nothing. `file_token` comes back non-optional
	precisely so that "the server said it worked" and "there is a File on the
	site" stop being the same sentence — a payload without it throws rather than
	letting a wizard report a document filed that is not.
	"""

	SWIFT = "OnboardingAPI.swift"
	STRICT = (("file_token", str, 228),)
	NULLABLE = (
		("file_url", str, 229),
		("file_name", str, 230),
		("is_private", bool, 231),
		("already_attached", bool, 234),
	)


class SignedI9Model(Codable):
	"""`OnboardingAPI.SignedI9` — the photograph of the signed sheet, filed.

	v0.48.3 gave this a struct. Until then the app called `upload_signed_i9`'s
	predecessor — a multipart POST at Frappe's own upload path — and read nothing
	back, which is how the file went nowhere without anybody hearing about it.
	Every field is optional because the screen shows a tick or an error and
	nothing else; `signed_pdf` is the one worth having, because it is the URL the
	I-9 Form now points at.
	"""

	SWIFT = "OnboardingAPI.swift"
	NULLABLE = (
		("name", str, 284),
		("status", str, 285),
		("signed_pdf", str, 287),
		("file_token", str, 288),
	)


class ResolvedBadgeModel(Codable):
	"""`ResolvedBadge` — v0.50.0, the read between a scan and a name.

	THREE STRICT FIELDS AND THE REST OPTIONAL, and the split is the contract.
	`badge_id`, `employee` and `employee_name` are what every caller needs — the
	docname `add_worker_to_shift` takes and the name that goes on screen — and a
	payload missing any of them cannot identify anybody, so it throws rather than
	degrading into a row that names nobody. Everything else is a decoration on
	that: a missing `designation` is a blank line on a card, not a mis-scan.

	`on_shift` IS A NULLABLE Bool AND NOT A LENIENT ONE, deliberately. Absent
	means the question was not asked — the caller passed no `shift` — and that is
	NOT the same as `false`. A client reading absence as false would refuse every
	scan on every sync that did not name a shift, which is most of them.
	"""

	SWIFT = "ResolvedBadge.swift"
	STRICT = (
		("badge_id", str, 23),
		("employee", str, 24),
		("employee_name", str, 25),
	)
	NULLABLE = (
		("employee_number", str, 27),
		("designation", str, 28),
		("status", str, 31),
		("company", str, 32),
		("photo_url", str, 33),
		("photo_placeholder", str, 37),
		("active", bool, 38),
		("shift", str, 41),
		("on_shift", bool, 45),
		("joined_at", str, 46),
	)


class IssuedBadgeModel(Codable):
	"""`IssuedBadge` — v0.51.0, the card the wizard's badge step now issues.

	TWO STRICT FIELDS. Without `badge_id` there is nothing to write on a card or
	into the register and without `employee` there is nobody it belongs to, so a
	payload missing either throws rather than degrading into a badge that names
	nobody. Everything else is decoration on that, and the decoration is exactly
	where a site legitimately differs: a farm that has not set `badge_logo` and
	a hire who declined a photograph both produce a perfectly good card, which
	is why `photo_placeholder` is on the wire beside `photo_url`.

	`png_base64` IS NULLABLE AND ITS ABSENCE IS NOT "A BADGE WITH NO QR". A
	bench with no encoder refuses the whole call — `registry` gates the tool on
	`_qr_available` and the direct path raises — so nothing here can answer with
	a badge and no code. Nullable records that an older server might not send
	the key, not that the card can ship without one.

	`created` AND `reused` ARE BOTH SENT and are not redundant on the wire even
	though one is the other's negation: the screen says "Badge Issued" or "Badge
	Reprinted", and a client inferring the second from the absence of the first
	would call every payload it failed to parse a reprint.
	"""

	SWIFT = "IssuedBadge.swift"
	STRICT = (
		("badge_id", str, 20),
		("employee", str, 21),
	)
	NULLABLE = (
		("employee_name", str, 23),
		("company", str, 24),
		("designation", str, 25),
		("created", bool, 31),
		("reused", bool, 32),
		("png_base64", str, 40),
		("png_bytes", int, 41),
		("photo_url", str, 46),
		("photo_placeholder", str, 47),
		("company_logo_url", str, 51),
	)


class EmployeePhotoModel(Codable):
	"""`EmployeePhoto` — v0.51.0, the headshot that makes a card carry a face.

	EVERY FIELD IS NULLABLE INCLUDING `image_set`, AND THAT IS THE CONTRACT.
	`image_set` is the one the caller acts on and it is NOT the same as success:
	a site whose Employee doctype has no `image` column files the photograph and
	answers `false`, meaning the bytes are on the record and the badge will
	still print initials. Reading a 200 as "the badge has a face on it now" is
	precisely the class of mistake that lost every onboarding upload before
	v0.48.3, and a strict field here would invite the opposite error — throwing
	away a successful file because an older server did not send the key.
	"""

	SWIFT = "IssuedBadge.swift"
	NULLABLE = (
		("employee", str, 82),
		("photo_url", str, 83),
		("file_token", str, 84),
		("file_name", str, 85),
		("image_set", bool, 86),
		("replaced", bool, 88),
		("already_attached", bool, 89),
	)


class OpenShiftRowModel(Codable):
	"""`ShiftAPI.OpenShift` — one row of the register, as the recovery read decodes it.

	EVERY FIELD BUT `name` IS OPTIONAL AND THAT IS THE POINT OF THE STRUCT. The
	docname is the only thing the handset actually needs: it is what `end_shift`
	takes, and the whole reason `list_shifts` reached this surface was a phone
	that had lost it. The rest is what the recovery screen shows a foreman so
	they can tell WHICH shift they are being asked to sign closed, and a missing
	block name is a thinner screen rather than a row that throws.

	`crew_size` is nil on this read and that is correct, not a gap: `shifts.describe`
	only walks the crew child table under `with_children`, which the register does
	not pass. `get_shift` is where the count comes from.
	"""

	SWIFT = "ShiftAPI.swift"
	STRICT = (("name", str, 18),)
	NULLABLE = (
		("foreman", str, 19),
		("foreman_name", str, 20),
		("company", str, 21),
		("start_datetime", str, 22),
		("crew_size", int, 23),
		("location", str, 31),
		("shift_type", str, 32),
		("open", bool, 35),
	)


class ShiftRegisterModel(Codable):
	"""`ShiftAPI.ShiftRegisterPage` — the answer `list_shifts` hands the handset.

	`shifts` is optional in Swift so an empty or absent list is a quiet "nothing
	of yours is open" rather than a throw — which is the ordinary answer and must
	not look like a fault. The register's own summary keys (`open`,
	`closed_without_a_signature`, the notes) are deliberately NOT transcribed: the
	phone asks a narrower question than they answer, and mirroring keys nothing
	decodes would assert a contract the app does not actually depend on.
	"""

	SWIFT = "ShiftAPI.swift"
	NULLABLE = (("count", int, 72),)
	NESTED = (("shifts", OpenShiftRowModel, True, 71),)


class FeedbackReceiptModel(Codable):
	"""`FeedbackAPI.Receipt` — the answer `submit_app_feedback` hands the handset.

	THE ONLY MIRROR IN THIS FILE WITH NOTHING STRICT IN IT, and the absence is
	the contract rather than a gap in it. `FeedbackAPI.docname(in:)` reads the
	reply with `JSONSerialization` and answers nil for any shape it does not
	recognise, and its own comment says why: the note has already been filed by
	the time that runs, so throwing because the envelope was `{"ok": true}`
	rather than `{"name": …}` would put it back in the queue and file it twice.
	A mirror that asserted a required key here would be asserting a promise the
	app deliberately does not make.

	What IS asserted is the one thing the app does read: `name`, if present, is a
	String. It tries `name`, `docname`, `feedback` and `app_feedback` in that
	order and takes the first non-empty string, so a docname emitted as an int —
	or as a nested object — is not a crash, it is a receipt with no name in it,
	which is a silent miss and therefore checked.
	"""

	SWIFT = "FeedbackAPI.swift"
	LENIENT = (("name", str, 113),)


class UndecodedResponseModel(Codable):
	"""A method `MobileAPI.swift` names and no Swift struct decodes yet.

	The crew clock and the bucket sync: v0.45.0 publishes the endpoints, and
	`MobileAPI` carries their paths, but `CrewClockSession` still keeps its shift
	on the handset and `BucketEntry` is still only ever written to the local queue.
	The mirror records that honestly rather than pretending to a contract, and
	holds the one line that IS already true — the answer is an object — so that
	whoever writes `Shift.swift` starts from a payload that can carry a struct.

	v0.47.0 puts two more here, and they are the case this class was written for
	rather than an exception to it. `list_i9_document_types` and `reverify_i9`
	PRECEDE their screens: the app's Section 2 picker is a hardcoded Swift array
	today and its wizard has no reverification step at all, so there is no struct
	to transcribe and inventing one would assert a contract neither side has —
	the sentence `VoidResponseModel` above already refuses to write. What the two
	tests covering them do instead is assert the PAYLOAD, key by key, because the
	shape is the thing being published and a picker built against it is the next
	thing anybody writes.

	v0.47.1 puts two more here for the same reason and one more besides. The I-9
	review screen and the Print button do not exist in the shipped app, so
	`get_i9_form` and `generate_i9_pdf` have no struct to mirror — and
	`generate_i9_pdf`'s answer is deliberately a NARROWED reshape of the tool's,
	not the tool's own dict, so the Swift written against it later is written
	against a stable dozen keys rather than against everything `render_i9_pdf`
	happens to return. The tests assert those keys one by one.

	v0.48.3 TOOK ONE BACK OUT, and the reason is worth reading before adding
	another. `upload_signed_i9` sat here because the app read nothing back from
	the call that filed a signed I-9 — and the call it was actually making,
	`FrappeClient.uploadFile`, checked the HTTP status and not the body. Frappe
	answers an unauthenticated API request with 200 and the Desk login page, so
	"reads nothing back" was not a gap in coverage, it was the bug: the phone
	could not tell a filed document from a lost one. It has `SignedI9Model` now.
	A method whose answer nobody decodes is a method whose failure nobody sees.
	"""

	SWIFT = "MobileAPI.swift"


# ── the wire ────────────────────────────────────────────────────────────────
def on_the_wire(value):
	"""What `frappe.as_json` would put on the phone's socket.

	Not decoration. `default=str` is what turns a `datetime` into the string
	`FrappeDate` parses, and asserting against the wrapper's own return value
	would stop one step before the conversion that half of this file is looking
	for.
	"""
	return json.loads(json.dumps(value, default=str))


class ContractTestCase(MobileAPITestCase):
	"""One enrolled worker, one camp, one inspection task on the board."""

	def setUp(self):
		super().setUp()
		self.unit = self.a_camp()
		self.task = self.a_task(
			creates_record="Housing Inspection",
			location_doctype="Housing Unit",
			location=self.unit,
		)
		self.be()

	def wire(self, method, **kwargs):
		"""Call one mobile method as the worker and return what the phone gets."""
		return on_the_wire(getattr(mobile_api, method)(**kwargs))

	def files_wire(self, method, **kwargs):
		return on_the_wire(getattr(files_api, method)(**kwargs))

	#: The person v0.45.0's onboarding methods are run against. Carries
	#: `first_name`/`last_name` and NOT `legal_*`, because the fallback in
	#: `submit_i9_section_1` reading them off the Employee row is the one thing
	#: that makes the shipped `OnboardingI9Section1.apiParams` — which sends
	#: neither legal name — a call that succeeds rather than a required-field
	#: refusal on a phone.
	NEW_HIRE = "EMP-NEWHIRE"
	SECOND_HAND = "EMP-SECOND"

	def the_hr_furniture(self):
		"""Everything the nine v0.45.0 methods need, and the role that reaches them.

		THE ROLE IS THE POINT OF THIS HELPER. `tools/i9.py`, `tools/w4.py`,
		`tools/shifts.py` and `tools/bucket_log.py` each gate on
		`employee.require_hr_role`, `employee.require_shift_role` or
		`kpi.require_kpi_role`, and the only role in ALL of those lists and
		`guard.FARM_OPS_ROLES` is Farm Manager. Ana is enrolled as a Field Worker
		like every other test in this file, so she is granted the second role here
		— which is exactly what an operator has to do on a real site before a phone
		can file an I-9. A fixture that widened the gate instead would be testing a
		site nobody runs.

		THE SHIFT TOOLS ARE THE ONE PLACE THAT IS NO LONGER THE ONLY WAY IN:
		`SHIFT_ROLES` adds Foreman and Crew Leader, so a supervisor enrolled as a
		Foreman opens and closes shifts without being made a Farm Manager. Farm
		Manager still reaches them, which is why this helper does not change.
		"""
		install_hrms()
		STORE.singles["I-9 Settings"] = {
			"doctype": "I-9 Settings",
			"store_document_copies": "0",
			"enrolled_in_e_verify": "0",
			"business_legal_name": "Test Farm LLC",
			"business_address": "123 Orchard Rd",
			"business_ein": "12-3456789",
			"reminder_days_before_doc_expiration": "90",
			"reminder_days_before_destruction": "60",
		}
		STORE.seed(
			"Employee",
			[
				{
					"name": self.NEW_HIRE,
					"employee_name": "Rosa Delgado",
					"first_name": "Rosa",
					"last_name": "Delgado",
					"company": MAIN,
					"status": "Active",
					"date_of_joining": frappe.utils.today(),
				},
				{
					"name": self.SECOND_HAND,
					"employee_name": "Marco Vega",
					"first_name": "Marco",
					"last_name": "Vega",
					"company": MAIN,
					"status": "Active",
					"date_of_joining": frappe.utils.today(),
				},
			],
		)
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()

	def a_shift(self, **overrides):
		"""One open shift, started by Ana from her own phone."""
		payload = {"location": "Block 7 North", "shift_type": "Harvest", "company": MAIN}
		payload.update(overrides)
		return self.wire("start_shift", **payload)

	def upload(self, kind="photo", name="FT_photo.jpg"):
		import base64
		import hashlib

		body = f"{kind}-bytes".encode()
		payload = base64.b64encode(body).decode()
		upload_id = f"{kind}-contract"
		staged = self.files_wire(
			"stage_file_chunk",
			upload_id=upload_id,
			file_name=name,
			chunk_index=0,
			chunk_count=1,
			total_bytes=len(body),
			data=payload,
		)
		finalized = self.files_wire(
			"finalize_staged_file",
			upload_id=upload_id,
			file_name=name,
			sha256=hashlib.sha256(body).hexdigest(),
			total_bytes=len(body),
		)
		return staged, finalized


# ── v0.62.0: the seven the handset named and this surface answered with a 404 ─
class OrgOptionModel(Codable):
	"""`OrgOption` — one Department, Designation or Employment Type row.

	`name` IS THE DOCNAME AND IT IS THE ONLY THING THAT TRAVELS BACK. All four
	masters are Link fields on Employee, so what `set_employee_org_fields` writes
	is a docname and not a label; Frappe spells the two differently often enough
	to matter (a Department docnamed `Orchard - OML` is labelled `Orchard`), and
	writing the label is a link-validation failure rather than a department.

	LENIENT THROUGHOUT, and the consequence is stated in the Swift: a row whose
	`name` decodes empty is FILTERED OUT of the picker rather than throwing. So a
	server that omitted the key would not crash the step — it would quietly offer
	a shorter list, which is the silent miss this file exists to catch.
	"""

	SWIFT = "OrgReferenceData.swift"
	LENIENT = (
		("name", str, 24),
		("label", str, 28),
	)


class BranchOptionModel(Codable):
	"""`BranchOption` — a Branch, and the ground it holds.

	`parcel` IS THE JOIN THE HOUSING STEP CANNOT BE REACHED WITHOUT. An Employee
	carries a Branch and a Housing Unit stands on a Parcel; `Parcel.branch` is the
	only column between them, and without it a wizard that has just asked which
	camp somebody works at cannot then show that camp's cabins.

	IT IS LENIENT AND NULL IS A REAL ANSWER. The server sends null for a branch
	that maps to NO parcels and for one that maps to SEVERAL — see
	`list_onboarding_reference_data` on why a scalar that picked the first of two
	would send half a camp's beds missing — and the client's documented fallback
	for null is to ask the server, which is what passing `branch` to
	`list_housing_units` does.
	"""

	SWIFT = "OrgReferenceData.swift"
	LENIENT = (
		("name", str, 76),
		("label", str, 77),
		("parcel", str, 80),
		("parcel_name", str, 83),
	)


class OrgReferenceDataModel(Codable):
	"""`OrgReferenceData` — the four dropdowns on the Assignment step, in one read.

	NO STRICT FIELD ANYWHERE, and that is the design rather than a weakness. A
	master this site does not have comes back as an empty list and the step drops
	that picker; a master that threw would mean a bench without Frappe HR's Branch
	doctype could not hire anybody at all. `isFromServer` is the flag that
	separates "this farm has no departments" from "the call failed", and the
	client sets it itself — it is not on the wire, which is why it is not here.
	"""

	SWIFT = "OrgReferenceData.swift"
	NESTED = (
		("branches", BranchOptionModel, True, 128),
		("departments", OrgOptionModel, True, 129),
		("designations", OrgOptionModel, True, 130),
		("employment_types", OrgOptionModel, True, 131),
	)


class AppliedOrgFieldsModel(Codable):
	"""`AppliedOrgFields` — what actually stuck, read back off the record.

	THE ECHO IS THE POINT AND IT IS WHY THIS METHOD ANSWERS AT ALL. All four are
	Link fields, and a site missing one of the doctypes — or an account without
	write permission on it — leaves that column unset while the call as a whole
	succeeds. A step that assumed its own state was filed would show a green tick
	over a department nobody has.

	`skipped` IS A LIST AND ITS ABSENCE READS AS "EVERYTHING LANDED", which is
	the older-server case the Swift's `decodeIfPresent` is written for. It is
	`update_employee`'s `fields_not_on_this_site` under the name the client uses.
	"""

	SWIFT = "OrgReferenceData.swift"
	NULLABLE = (
		("employee", str, 320),
		("branch", str, 321),
		("department", str, 322),
		("designation", str, 323),
		("employment_type", str, 324),
	)
	LENIENT = (("skipped", list, 328),)


class HousingUnitModel(Codable):
	"""`HousingUnit` — one cabin, with the beds in it and the reason if there are none.

	LENIENT THROUGHOUT, and the Swift says why in its own words: one unreadable
	cabin must not empty the camp list on a phone in a yard. What that costs is
	that every miss here is SILENT, which is exactly why the keys are checked.

	`capacity` AND `occupied` ARE NON-OPTIONAL Ints ON THE SWIFT SIDE AND STILL
	LENIENT ON THE WIRE — `lenientInt(...) ?? 0`. So the server's `null` for an
	unmeasured cabin arrives as 0, which is what `open_beds: null` already means
	and is not "every bed taken".

	`occupied` IS READ FIRST AND `current_occupants` SECOND, which is why both are
	on the wire. `assignable` DEFAULTS TO TRUE when the server is silent — a
	client greying out every unit because a field was missing is a camp list
	nobody can use — so `blocked_reason` beside it is what makes a refusal legible
	BEFORE the tap rather than after it.
	"""

	SWIFT = "HousingModels.swift"
	LENIENT = (
		("name", str, 24),
		("unit_name", str, 28),
		("unit_type", str, 32),
		("parcel", str, 34),
		("parcel_name", str, 35),
		("capacity", int, 38),
		("occupied", int, 41),
		("current_occupants", int, 41),
		("open_beds", int, 45),
		("condition", str, 48),
		("max_occupants_per_or_law", int, 54),
		("is_residential", bool, 57),
		("assignable", bool, 60),
		("blocked_reason", str, 63),
	)


class HousingUnitListModel(Codable):
	"""`HousingUnitList` — the camp, and the totals the step's header prints.

	`total_open_beds` IS THE WHOLE CAMP AND NOT THE ROWS RETURNED. The header
	says "6 beds open across 4 cabins", and computing that from a filtered or
	truncated list would be a lie the size of the filter.

	`units` PRESENT AND EMPTY IS A REAL ANSWER — a ranch with no camp — and the
	Swift checks for the KEY rather than for content precisely so that it does
	not fall through to its bare-array tolerance and throw over a correct
	response.
	"""

	SWIFT = "HousingModels.swift"
	LENIENT = (
		("total_open_beds", int, 209),
		("parcel", str, 213),
		("parcel_name", str, 214),
	)
	NESTED = (("units", HousingUnitModel, True, 206),)


class HousingAssignmentResultModel(Codable):
	"""`HousingAssignmentResult` — the row that was written, and the beds left.

	`name` IS THE ONE FIELD THE CLIENT READS WITHOUT A FALLBACK, and it is the
	Housing Assignment docname. The Swift decodes it leniently into `""`, so an
	answer missing it is not a crash — it is an assignment the step cannot name,
	on a record that exists, which is the harder failure to notice. The key is
	checked here for that reason.

	THE OCCUPANCY COMES BACK SO THE LIST IS NOT RE-FETCHED. A foreman housing a
	crew of six assigns six times off one screen, and a re-read between each is
	six round trips in a place with one bar of signal.
	"""

	SWIFT = "HousingModels.swift"
	LENIENT = (
		("name", str, 294),
		("unit", str, 295),
		("unit_name", str, 296),
		("employee", str, 297),
		("assigned_date", str, 298),
		("occupied", int, 301),
		("open_beds", int, 302),
	)


class EmployeeAttachmentModel(Codable):
	"""`EmployeeAttachment` — one file in the folder.

	`name` IS THE ONLY STRICT FIELD ON THE WHOLE ROW, and it is strict in the
	Swift too: `try c.decode(String.self, forKey: .name)`. It is the File DOCNAME
	and it is what `get_attachment_content` is asked for, so a row without one is
	a row that cannot be opened — the v0.18.2 class of failure, where the list
	renders and the tap throws.

	`file_name` FALLS BACK TO THE DOCNAME, which is the client's rule and not a
	licence to omit it: a File row with no filename is still a file somebody can
	open, and the docname is the only other handle on it.

	`content_type` IS THE CLIENT'S SPELLING OF `mime_type`. Both are sent; this
	mirror checks the one the Swift actually reads.

	`document_kind` IS ABSENT ON PURPOSE AND IS NOT CHECKED HERE.
	`attach_onboarding_document` records the label on the AUDIT ROW rather than
	on the File — it is a label on the act, not a column on the object — so
	reporting one would mean inventing it. The client treats it as optional and
	shows the filename instead.
	"""

	SWIFT = "EmployeeAttachment.swift"
	STRICT = (("name", str, 16),)
	LENIENT = (
		("file_name", str, 19),
		("file_url", str, 24),
		("file_size", int, 26),
		("is_private", bool, 27),
		("content_type", str, 28),
	)
	DATES = (("creation", 29),)


class AttachmentContentModel(Codable):
	"""`AttachmentContent` — the bytes, base64, in one answer.

	`content` IS THE CONTRACT AND `data` IS ACCEPTED, per the client's own
	decoder; this surface sends `content` and `content_base64`, because the MCP
	tool has called it the second since v0.1 and neither name is being taken away
	from anybody.

	THE BYTES TRAVEL RATHER THAN A URL, which is the whole reason the method
	exists: every file this app writes is private, the handset authenticates to
	the sidecar rather than to Frappe, and a `/private/files/…` link answers that
	with a login page. A payload with no `content` in it is a viewer with nothing
	to draw.
	"""

	SWIFT = "AttachmentAPI.swift"
	LENIENT = (
		("file_name", str, 71),
		("content_type", str, 72),
		("content", str, 73),
	)


# ── 1. the twelve methods, each decoded by its mirror ─────────────────────────
class EveryMobileMethodDecodes(ContractTestCase):
	"""The suite proper. One test per method the app calls."""

	def test_01_get_current_user_context(self):
		UserContextModel.decode(self.wire("get_current_user_context"), "get_current_user_context")

	def test_02_list_my_tasks(self):
		mobile_api.claim_task(task=self.task)
		body = self.wire("list_my_tasks")
		self.assertTrue(body["tasks"], "the fixture claimed a task and the list came back empty")
		for index, row in enumerate(body["tasks"]):
			FarmTaskModel.decode(row, "list_my_tasks", f".tasks[{index}]")

	def test_03_list_available_tasks(self):
		body = self.wire("list_available_tasks")
		self.assertTrue(body["tasks"], "the fixture put a task in the pool and the pool came back empty")
		for index, row in enumerate(body["tasks"]):
			FarmTaskModel.decode(row, "list_available_tasks", f".tasks[{index}]")

	def test_04_get_task(self):
		FarmTaskModel.decode(self.wire("get_task", task=self.task), "get_task")

	def test_05_claim_task(self):
		"""THE v0.18.2 REGRESSION TEST. This is the call that shipped broken."""
		row = self.wire("claim_task", task=self.task)
		FarmTaskModel.decode(row, "claim_task")
		self.assertEqual(row["name"], self.task)
		self.assertTrue(row["assignment"], "a claim with no assignment is not a claim")

	def test_06_start_task(self):
		mobile_api.claim_task(task=self.task)
		row = self.wire("start_task", task=self.task)
		FarmTaskModel.decode(row, "start_task")
		self.assertEqual(row["state"], "In-Progress")

	def test_07_complete_task_via_mobile(self):
		claimed = mobile_api.claim_task(task=self.task)
		mobile_api.start_task(task=self.task)
		_staged, photo = self.upload("photo", "FT_photo.jpg")
		_staged, signature = self.upload("signature", "FT_signature.png")
		row = self.wire(
			"complete_task_via_mobile",
			task=self.task,
			task_assignment=claimed["assignment"],
			findings_text="",
			clean_pass=True,
			completion_narrative="walked it",
			evidence_files=[
				{"file_token": photo["file_token"], "file_name": "FT_photo.jpg", "kind": "photo"},
				{
					"file_token": signature["file_token"],
					"file_name": "FT_signature.png",
					"kind": "signature",
				},
			],
		)
		CompletionResultModel.decode(row, "complete_task_via_mobile")
		self.assertEqual(row["created_record_doctype"], "Housing Inspection")

	def test_08_reject_task(self):
		mobile_api.claim_task(task=self.task)
		row = self.wire("reject_task", task=self.task, reason="the ladder is broken")
		RejectionModel.decode(row, "reject_task")
		self.assertEqual(row["task"], self.task)

	def test_08a_pause_and_resume_task_via_mobile(self):
		"""THE v0.90.0 REGRESSION TEST. Both wrappers shipped calling the module
		helper `_assignment(task, task_assignment, allowed)` with a FOURTH
		positional argument — `_assignment(task, task_assignment, [], "task")` —
		which is a `TypeError` on the call itself, before any state logic. Every
		pause and every resume answered HTTP 500 on the live sidecar (found
		2026-08-17 running the iOS end-to-end test against Umbrel), and nothing
		here caught it because `test_api_mobile` only asserts these two names are
		*registered* — it never *invokes* them. The dispatch tool underneath was
		always fine; `test_task_interruption` exercises that and stayed green.
		This calls the wrappers the way a phone does.
		"""
		claimed = mobile_api.claim_task(task=self.task)
		mobile_api.start_task(task=self.task)

		# Pause the way the app does: task named, assignment alongside it, a reason.
		paused = self.wire(
			"pause_task_via_mobile",
			task=self.task,
			task_assignment=claimed["assignment"],
			reason="Called to the valve at Home-7",
		)
		self.assertEqual(paused["state"], "Paused")
		self.assertEqual(paused["reason"], "Called to the valve at Home-7")
		self.assertEqual(self.wire("get_task", task=self.task)["state"], "Paused")

		# And task-only, no assignment — the other branch of the same helper, so
		# the fix is proven on both paths rather than just the one the app favours.
		resumed = self.wire("resume_task_via_mobile", task=self.task)
		self.assertEqual(resumed["state"], "In-Progress")
		self.assertEqual(self.wire("get_task", task=self.task)["state"], "In-Progress")

	def test_09_list_compliance_alerts(self):
		# The sweep is an operator's call, not a worker's — the phone only ever
		# reads the calendar it produces.
		frappe.local.session.user = "Administrator"
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})
		self.be()
		body = self.wire("list_compliance_alerts")
		for index, row in enumerate(body["alerts"]):
			ComplianceAlertSummaryModel.decode(row, "list_compliance_alerts", f".alerts[{index}]")

	def test_10_report_field_task(self):
		_staged, photo = self.upload("photo", "FR_photo.jpg")
		row = self.wire(
			"report_field_task",
			photo_file_token=photo["file_token"],
			description="broken sprinkler head",
		)
		FarmTaskModel.decode(row, "report_field_task")

	def test_11_stage_file_chunk(self):
		staged, _finalized = self.upload()
		StagedChunkModel.decode(staged, "stage_file_chunk")
		self.assertEqual(staged["chunk_index"], 0)
		self.assertTrue(staged["complete"])

	def test_12_finalize_staged_file(self):
		_staged, finalized = self.upload()
		FinalizedFileModel.decode(finalized, "finalize_staged_file")
		self.assertTrue(finalized["file_token"])

	def test_13_scan_asset(self):
		self.configure(enabled=1, allow_scan_asset=1, allow_register_asset=1)
		self.tool_data(
			"register_asset",
			{
				"name": "MC-Valve-05",
				"asset_type": "Irrigation Valve",
				"company": MAIN,
			},
		)
		self.be()
		row = self.wire("scan_asset", asset_name="MC-Valve-05")
		ScanAssetModel.decode(row, "scan_asset")
		self.assertTrue(row["scan_recorded"])

	def test_14_get_asset_detail(self):
		self.configure(enabled=1, allow_get_asset_detail=1, allow_register_asset=1)
		self.tool_data(
			"register_asset",
			{
				"name": "MC-Valve-05",
				"asset_type": "Irrigation Valve",
				"company": MAIN,
			},
		)
		self.be()
		row = self.wire("get_asset_detail", asset_name="MC-Valve-05")
		AssetDetailModel.decode(row, "get_asset_detail")
		self.assertEqual(row["name"], "MC-Valve-05")

	def test_15_log_asset_state_change(self):
		self.configure(enabled=1, allow_log_asset_state_change=1, allow_register_asset=1)
		self.tool_data(
			"register_asset",
			{
				"name": "MC-Valve-05",
				"asset_type": "Irrigation Valve",
				"company": MAIN,
			},
		)
		self.be()
		row = self.wire("log_asset_state_change", asset_name="MC-Valve-05", action="open_valve")
		StateChangeModel.decode(row, "log_asset_state_change")
		self.assertEqual(row["from_state"], "closed")
		self.assertEqual(row["to_state"], "open")

	def test_16_get_available_actions(self):
		self.configure(enabled=1, allow_get_available_actions=1, allow_register_asset=1)
		self.tool_data(
			"register_asset",
			{
				"name": "MC-Valve-05",
				"asset_type": "Irrigation Valve",
				"company": MAIN,
			},
		)
		self.be()
		row = self.wire("get_available_actions", asset_name="MC-Valve-05")
		AvailableActionsModel.decode(row, "get_available_actions")
		self.assertEqual(row["current_state"], "closed")

	def test_17_report_asset_issue(self):
		self.configure(
			enabled=1,
			allow_report_asset_issue=1,
			allow_register_asset=1,
			allow_report_field_task=1,
		)
		self.tool_data(
			"register_asset",
			{
				"name": "MC-Valve-05",
				"asset_type": "Irrigation Valve",
				"company": MAIN,
			},
		)
		self.be()
		_staged, photo = self.upload("photo", "AI_photo.jpg")
		row = self.wire(
			"report_asset_issue",
			asset_name="MC-Valve-05",
			description="Leaking badly",
			photo_file_token=photo["file_token"],
		)
		FarmTaskModel.decode(row, "report_asset_issue")

	# ── v0.45.0: onboarding ─────────────────────────────────────────────────
	def test_18_create_i9_form(self):
		self.the_hr_furniture()
		row = self.wire(
			"create_i9_form",
			employee=self.NEW_HIRE,
			company=MAIN,
			hire_date=frappe.utils.today(),
		)
		VoidResponseModel.decode(row, "create_i9_form")
		self.assertEqual(row["status"], "Draft")

	def test_19_submit_i9_section_1(self):
		"""THE LEGAL NAMES ARE NOT SENT, exactly as the shipped app does not send them.

		`OnboardingI9Section1.apiParams` has no `legal_first_name` and no
		`legal_last_name`, and the tool requires both. If this test ever passes them
		it stops testing the call the phone actually makes.
		"""
		self.the_hr_furniture()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		row = self.wire(
			"submit_i9_section_1",
			employee=self.NEW_HIRE,
			citizenship_status="US Citizen",
			ssn_last_four="6789",
			address_street="14 Orchard Lane",
			address_city="Hood River",
			address_state="OR",
			address_zip="97031",
		)
		VoidResponseModel.decode(row, "submit_i9_section_1")
		self.assertEqual(row["status"], "Section 1 Complete")
		filed = next(iter(STORE.rows("I-9 Form")))
		self.assertEqual(filed["legal_first_name"], "Rosa")
		self.assertEqual(filed["legal_last_name"], "Delgado")

	def test_20_submit_i9_section_2(self):
		"""The three renames per document list, run through the List B + C path."""
		self.the_hr_furniture()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		self.wire("submit_i9_section_1", employee=self.NEW_HIRE, citizenship_status="US Citizen")
		row = self.wire(
			"submit_i9_section_2",
			employee=self.NEW_HIRE,
			document_path="List B + C",
			verifier_name="Ana Ramos",
			verifier_title="Farm Manager",
			verification_date=frappe.utils.today(),
			list_b_doc_type="Driver's License",
			list_b_doc_number="OR-4417",
			list_b_authority="Oregon DMV",
			list_c_doc_type="Social Security Card (Unrestricted)",
			list_c_doc_number="***-**-6789",
			list_c_authority="SSA",
		)
		VoidResponseModel.decode(row, "submit_i9_section_2")
		filed = next(iter(STORE.rows("I-9 Form")))
		self.assertEqual(filed["list_b_doc_title"], "Driver's License")
		self.assertEqual(filed["list_c_doc_authority"], "SSA")

		# v0.64.2. THIS IS THE CONTRACT SAYING WHAT THE APP IS MISSING, and the
		# assertion is left pointing at it rather than fixed up with a signature
		# the wizard does not send.
		#
		# `OnboardingWizardViewModel` posts the Section 1 and Section 2 DATA and
		# neither attestation: Section 1's capture is uploaded as a loose PNG on
		# the Employee, and Section 2's is drawn on the pad, shown as "Captured"
		# on the Review screen and never sent anywhere at all. So this call — the
		# app's own call, with the app's own arguments — leaves a Form I-9 with
		# its documents examined and nobody's name sworn to them.
		#
		# Until v0.64.2 that reached `Complete`, and the two Critical alerts that
		# followed were the only thing saying otherwise. It now rests at
		# `Awaiting Verification` and says which attestations are outstanding.
		self.assertEqual(row["status"], "Awaiting Verification")
		self.assertEqual(
			row["unsigned"],
			["Section 1 (the employee's attestation)", "Section 2 (the employer's attestation)"],
		)
		self.assertIn("8 CFR", row["unsigned_note"])

	def test_20a_the_form_completes_when_both_attestations_arrive(self):
		"""The other half of test 20, and what the wizard has to start doing.

		Two calls to `submit_form_signature` on the SAME handset and the same
		authenticated account — the employee's Section 1, identified by their own
		badge, and the phone owner's Section 2, authorised by the signer roster.
		The server has supported both since v0.60.0; nothing in the app makes
		either call on this path.
		"""
		self.the_hr_furniture()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		self.wire("submit_i9_section_1", employee=self.NEW_HIRE, citizenship_status="US Citizen")
		self.wire(
			"submit_i9_section_2",
			employee=self.NEW_HIRE,
			document_path="List A",
			verifier_name="Ana Ramos",
			verifier_title="Farm Manager",
			verification_date=frappe.utils.today(),
			list_a_doc_type="U.S. Passport",
			list_a_doc_number="X1234567",
			list_a_authority="US Dept of State",
		)
		form = str(next(iter(STORE.rows("I-9 Form")))["name"])

		first = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname=form,
			signature_field="section_1_signature",
			signature_image=A_PNG,
		)
		# The employee's attestation alone does not complete the form.
		self.assertIsNone(first["form_status_advanced_to"])
		self.assertEqual(first["form_status"], "Awaiting Verification")

		second = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname=form,
			signature_field="section_2_signature",
			signature_image=A_PNG,
		)
		# The employer's completes it, and the form says so without anybody
		# calling submit_i9_section_2 a second time.
		self.assertEqual(second["form_status_advanced_to"], "Complete")
		self.assertEqual(second["form_status"], "Complete")

		filed = next(iter(STORE.rows("I-9 Form")))
		self.assertEqual(filed["status"], "Complete")
		self.assertTrue(filed["section_1_signature"])
		self.assertTrue(filed["section_2_signature"])
		# Two signers, two capacities, on one form and one device.
		roles = sorted(
			str(row["signature_role"])
			for row in STORE.rows("Signing Evidence")
			if row["document_name"] == form
		)
		self.assertEqual(roles, ["Employee", "Employer Representative"])

	def test_21_submit_w4(self):
		self.the_hr_furniture()
		row = self.wire(
			"submit_w4",
			employee=self.NEW_HIRE,
			company=MAIN,
			tax_year=2026,
			filing_status="Married Filing Jointly",
			multiple_jobs=False,
			dependents_under_17=2,
			other_dependents=1,
			extra_withholding=25,
		)
		VoidResponseModel.decode(row, "submit_w4")
		self.assertEqual(row["status"], "Active")
		filed = next(iter(STORE.rows("W-4 Form")))
		self.assertEqual(int(filed["dependents_under_17_count"]), 2)
		self.assertEqual(float(filed["extra_withholding_per_period"]), 25.0)

	def test_22_link_badge_to_employee(self):
		self.the_hr_furniture()
		row = self.wire(
			"link_badge_to_employee",
			badge_id="QR-0042",
			employee=self.NEW_HIRE,
			company=MAIN,
		)
		VoidResponseModel.decode(row, "link_badge_to_employee")
		self.assertEqual(row["badge"]["employee"], self.NEW_HIRE)
		self.assertTrue(row["badge"]["active"])

	# ── v0.50.0: the read between a scan and a name ─────────────────────────
	def test_43_resolve_badge(self):
		"""`ResolvedBadge` — three strict fields, and the refusals are sentences.

		THE CALL EVERY BADGE-SHAPED FEATURE WAS BLOCKED ON. `add_worker_to_shift`
		takes an Employee docname and `QRScannerModel.handleScan` produces the
		raw scanned string, so the crew clock could scan a whole crew and roster
		none of it — and the bucket loop could show a foreman the code it read
		and never the picker's name.
		"""
		self.the_hr_furniture()
		self.wire("link_badge_to_employee", badge_id="QR-0042", employee=self.NEW_HIRE, company=MAIN)

		row = self.wire("resolve_badge", badge_id="QR-0042", company=MAIN)
		ResolvedBadgeModel.decode(row, "resolve_badge")
		self.assertEqual(row["employee"], self.NEW_HIRE)
		self.assertEqual(row["employee_name"], "Rosa Delgado")
		self.assertTrue(row["active"])
		# The question was not asked, so the answer must not claim it was.
		self.assertNotIn("on_shift", row)

	def test_43_resolve_badge_on_a_shift_answers_whether_they_are_clocked_in(self):
		self.the_hr_furniture()
		self.wire("link_badge_to_employee", badge_id="QR-0042", employee=self.NEW_HIRE, company=MAIN)
		shift = self.a_shift(crew_employees=[self.NEW_HIRE])

		row = self.wire("resolve_badge", badge_id="QR-0042", company=MAIN, shift=shift["name"])
		ResolvedBadgeModel.decode(row, "resolve_badge")
		self.assertTrue(row["on_shift"])
		self.assertEqual(row["shift"], shift["name"])

	def test_43_resolve_badge_refuses_a_scan_that_is_not_a_badge(self):
		"""A login QR held in front of the badge scanner used to become a badge
		ID with an api_secret inside it. It is refused before the register is
		even read."""
		self.the_hr_furniture()
		payload = '{"url":"https://erp.example.com","api_secret":"s3cr3t"}'
		with self.assertRaises(frappe.ValidationError) as caught:
			self.wire("resolve_badge", badge_id=payload, company=MAIN)
		self.assertIn("is not a badge ID", str(caught.exception))
		# THE REFUSAL DOES NOT ECHO WHAT IT REFUSED. This branch exists for a
		# `generate_mobile_login_qr` payload scanned by mistake; quoting it back
		# would put a live api_secret in a ValidationError, on a screen in an
		# orchard, and in the audit row below.
		self.assertNotIn("s3cr3t", str(caught.exception))
		row = [row for row in STORE.rows("MCP Action Log") if row.get("tool_name") == "mobile:resolve_badge"][
			-1
		]
		self.assertNotIn("s3cr3t", str(row.get("arguments_json")))
		self.assertIn("redacted", str(row.get("arguments_json")))

	def test_43_resolve_badge_refuses_an_unissued_badge_by_name(self):
		self.the_hr_furniture()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.wire("resolve_badge", badge_id="QR-9999", company=MAIN)
		self.assertIn("QR-9999", str(caught.exception))

	# ── v0.45.0: the capture queue and the crew clock ───────────────────────
	def test_23_sync_bucket_entries(self):
		"""The handset's own spelling — `id`, `session_id`, `badge_id`, `accepted`."""
		self.the_hr_furniture()
		self.wire("link_badge_to_employee", badge_id="QR-0042", employee=self.NEW_HIRE, company=MAIN)
		row = self.wire(
			"sync_bucket_entries",
			company=MAIN,
			entries=[
				{
					"id": "1E4A0C7A-0001",
					"session_id": "SESSION-A",
					"badge_id": "QR-0042",
					"timestamp": f"{frappe.utils.today()} 07:12:00",
					"accepted": True,
					"coverage_percent": 94.5,
					"device_id": "iPad-7",
				},
				{
					"id": "1E4A0C7A-0002",
					"session_id": "SESSION-A",
					"badge_id": "QR-0042",
					"timestamp": f"{frappe.utils.today()} 07:19:00",
					"accepted": False,
					"coverage_percent": 41.0,
					"device_id": "iPad-7",
				},
			],
		)
		UndecodedResponseModel.decode(row, "sync_bucket_entries")
		self.assertEqual(row["created_count"], 2)
		self.assertEqual(row["invalid_count"], 0, row["invalid"])
		filed = {entry["verdict"] for entry in STORE.rows("Bucket Log Entry")}
		self.assertEqual(filed, {"Accepted", "Rejected"})
		# The badge resolved through the map rather than through the body.
		self.assertTrue(all(entry["employee"] == self.NEW_HIRE for entry in STORE.rows("Bucket Log Entry")))

	def test_23_the_timestamp_the_handset_actually_sends(self):
		"""v0.59.2 — ISO 8601 WITH A TRAILING Z, WHICH IS THE ONLY THING A PHONE SENDS.

		THE TEST ABOVE IS WHY THIS BUG SHIPPED. `test_23` feeds
		`f"{today()} 07:12:00"` — a Frappe Datetime string, the shape a Desk
		import has — and no handset has ever produced one.
		`BadgeAPI.payload` stamps every capture with an `ISO8601DateFormatter`
		set to `.withInternetDateTime` in UTC (`BadgeAPI.timestamps`), so the
		wire carries `2026-08-11T07:12:00Z`, and a MariaDB DATETIME will not take
		the `T` or the `Z`. Both entries in the batch above passed, the endpoint
		was published, and not one bucket entry ever reached the register from a
		device — 31 sat queued on one iPad.

		The payload below is `BadgeAPI.payload` transcribed field for field,
		`capture_mode` and `auto_verdict` included, because a fixture that sent
		only the fields the server reads would not have caught this one either.
		"""
		self.the_hr_furniture()
		self.wire("link_badge_to_employee", badge_id="QR-0042", employee=self.NEW_HIRE, company=MAIN)
		row = self.wire(
			"sync_bucket_entries",
			company=MAIN,
			entries=[
				{
					"id": "1E4A0C7A-0001",
					"session_id": "SESSION-A",
					"badge_id": "QR-0042",
					"timestamp": "2026-08-11T07:12:00Z",
					"accepted": True,
					"coverage_percent": 94.5,
					"device_id": "iPad-7",
					"auto_verdict": "full",
					"capture_mode": "ML Verified",
				},
				{
					"id": "1E4A0C7A-0002",
					"session_id": "SESSION-A",
					"badge_id": "QR-0042",
					"timestamp": "2026-08-11T07:19:31Z",
					"accepted": False,
					"coverage_percent": 41.0,
					"device_id": "iPad-7",
					"auto_verdict": "not_full",
					"capture_mode": "ML Verified",
				},
			],
		)
		UndecodedResponseModel.decode(row, "sync_bucket_entries")
		self.assertEqual(row["invalid_count"], 0, row["invalid"])
		self.assertEqual(row["created_count"], 2)

		# THE INSTANT SURVIVES, IN THE SHAPE THE COLUMN TAKES. Not the sync
		# time, not midnight, not the string it arrived as.
		filed = sorted(entry["timestamp"] for entry in STORE.rows("Bucket Log Entry"))
		self.assertEqual(filed, ["2026-08-11 07:12:00", "2026-08-11 07:19:31"])

		# And the session the two belong to was aggregated off those timestamps,
		# which is the read a foreman actually opens.
		session = STORE.rows("Bucket Log Session")[0]
		self.assertEqual(session["started_at"], "2026-08-11 07:12:00")
		self.assertEqual(session["ended_at"], "2026-08-11 07:19:31")
		self.assertEqual(session["total_accepted"], 1)
		self.assertEqual(session["total_rejected"], 1)

	def test_23_an_offset_is_applied_rather_than_dropped(self):
		"""A handset whose formatter is not on UTC still files the right instant.

		`BadgeAPI.timestamps` pins UTC, so `+00:00` is what production sends —
		but `ISO8601DateFormatter` is one line from emitting a local offset, and
		the rule that a `+02:00` capture is stored two hours earlier rather than
		at its wall clock is `datetimes.as_mariadb_datetime`'s, checked here at
		the boundary that now depends on it.
		"""
		self.the_hr_furniture()
		self.wire("link_badge_to_employee", badge_id="QR-0042", employee=self.NEW_HIRE, company=MAIN)
		row = self.wire(
			"sync_bucket_entries",
			company=MAIN,
			entries=[
				{
					"id": "1E4A0C7A-0003",
					"session_id": "SESSION-B",
					"badge_id": "QR-0042",
					"timestamp": "2026-08-11T09:12:00+02:00",
					"accepted": True,
					"device_id": "iPad-7",
				}
			],
		)
		self.assertEqual(row["invalid_count"], 0, row["invalid"])
		self.assertEqual(STORE.rows("Bucket Log Entry")[0]["timestamp"], "2026-08-11 07:12:00")

	def test_24_start_shift(self):
		"""`foreman` IS NOT IN THE SIGNATURE — it comes off the authenticated caller."""
		self.the_hr_furniture()
		row = self.a_shift(crew_employees=[self.NEW_HIRE, self.SECOND_HAND])
		UndecodedResponseModel.decode(row, "start_shift")
		self.assertEqual(row["foreman"], "EMP-ANA")
		self.assertEqual(row["crew_size"], 2)

	def test_25_add_worker_to_shift(self):
		self.the_hr_furniture()
		shift = self.a_shift(crew_employees=[self.NEW_HIRE])
		row = self.wire("add_worker_to_shift", shift=shift["name"], employee=self.SECOND_HAND)
		UndecodedResponseModel.decode(row, "add_worker_to_shift")
		self.assertEqual(row["added"]["employee"], self.SECOND_HAND)
		self.assertEqual(row["crew_size"], 2)

	def test_26_end_shift(self):
		self.the_hr_furniture()
		shift = self.a_shift(crew_employees=[self.NEW_HIRE, self.SECOND_HAND])
		_staged, signature = self.upload("signature", "shift_signature.png")
		row = self.wire(
			"end_shift",
			shift=shift["name"],
			supervisor_signature_file_token=signature["file_token"],
		)
		UndecodedResponseModel.decode(row, "end_shift")
		self.assertEqual(row["attendance_created"], 2)

	# ── v0.46.0: the Identity step the wizard 404'd on ──────────────────────
	def test_27_create_employee(self):
		"""THE CALL THE WHOLE FLOW WAS STUCK BEHIND. Step 1 of five."""
		self.the_hr_furniture()
		row = self.wire(
			"create_employee",
			first_name="Elena",
			last_name="Marquez",
			employee_name="Elena Marquez",
			gender="Female",
			date_of_birth="1994-03-11",
			company=MAIN,
		)
		CreatedEmployeeModel.decode(row, "create_employee")
		filed = STORE.tables["Employee"][row["name"]]
		self.assertEqual(filed["employee_name"], "Elena Marquez")
		self.assertEqual(filed["company"], MAIN)
		self.assertEqual(filed["status"], "Active")

	def test_27_the_middle_name_off_the_licence_reaches_the_record(self):
		"""v0.51.0. The handset has parsed AAMVA's `DAD` since the ID scanner
		shipped and `middle_name` was not on `employee.WRITABLE`, so it was read
		off the card at the tailgate and dropped on arrival."""
		self.the_hr_furniture()
		row = self.wire(
			"create_employee",
			first_name="Elena",
			middle_name="Sofia",
			last_name="Marquez",
			employee_name="Elena Marquez",
			company=MAIN,
		)
		filed = STORE.tables["Employee"][row["name"]]
		self.assertEqual(filed["middle_name"], "Sofia")
		# NOT folded into the display name: `employee_name` is what a dispatch
		# board, a payroll register and the returning-worker search match on.
		self.assertEqual(filed["employee_name"], "Elena Marquez")

	def test_27_a_hire_with_no_middle_name_is_not_a_refusal(self):
		"""Most people have none, and a wizard that stopped on the field would
		stop on one the worker cannot fill."""
		self.the_hr_furniture()
		row = self.wire(
			"create_employee",
			first_name="Elena",
			last_name="Marquez",
			employee_name="Elena Marquez",
			company=MAIN,
		)
		self.assertFalse(STORE.tables["Employee"][row["name"]].get("middle_name"))

	def test_27_the_middle_name_then_fills_the_i9s_legal_middle_name(self):
		"""THE JOIN THAT MAKES IT WORTH DOING. `submit_i9_section_1` has filled
		Legal Middle Name from `Employee.middle_name` since v0.45.0 when the
		caller sends none — and the column it read could never be written, so
		that fallback resolved to empty on every I-9 this app has ever filed."""
		self.the_hr_furniture()
		created = self.wire(
			"create_employee",
			first_name="Elena",
			middle_name="Sofia",
			last_name="Marquez",
			employee_name="Elena Marquez",
			company=MAIN,
		)
		self.wire("create_i9_form", employee=created["name"], company=MAIN, hire_date=frappe.utils.today())
		self.wire(
			"submit_i9_section_1",
			employee=created["name"],
			citizenship_status="US Citizen",
			address_street="1420 Orchard Road",
			address_city="Yakima",
			address_state="WA",
			address_zip="98901",
		)
		form = next(
			row for row in STORE.tables["I-9 Form"].values() if row.get("employee") == created["name"]
		)
		self.assertEqual(form["legal_middle_name"], "Sofia")

	def test_28_search_employees(self):
		"""The list the foreman picks a returning picker out of, Left ones included."""
		self.the_hr_furniture()
		STORE.seed(
			"Employee",
			[
				{
					"name": "EMP-RETURNED",
					"employee_name": "Rosa Aguilar",
					"first_name": "Rosa",
					"last_name": "Aguilar",
					"company": MAIN,
					"status": "Left",
					"date_of_joining": "2025-06-02",
					"employment_type": "Seasonal",
				}
			],
		)
		body = self.wire("search_employees", query="Rosa", company=MAIN)
		self.assertTrue(body["employees"], "the fixture seeded two Rosas and the search came back empty")
		for index, row in enumerate(body["employees"]):
			ExistingEmployeeModel.decode(row, "search_employees", f".employees[{index}]")
		found = {row["name"]: row for row in body["employees"]}
		# The one who LEFT is the one this search exists to find.
		self.assertEqual(found["EMP-RETURNED"]["status"], "Left")
		self.assertEqual(found["EMP-RETURNED"]["employment_type"], "Seasonal")
		self.assertIn(self.NEW_HIRE, found)

	def test_29_reactivate_employee(self):
		"""The other branch: one record with a history, not two with one between them."""
		self.the_hr_furniture()
		STORE.seed(
			"Employee",
			[
				{
					"name": "EMP-RETURNED",
					"employee_name": "Rosa Aguilar",
					"first_name": "Rosa",
					"last_name": "Aguilar",
					"company": MAIN,
					"status": "Left",
					"date_of_joining": "2025-06-02",
				}
			],
		)
		row = self.wire("reactivate_employee", employee="EMP-RETURNED")
		VoidResponseModel.decode(row, "reactivate_employee")
		filed = STORE.tables["Employee"]["EMP-RETURNED"]
		self.assertEqual(filed["status"], "Active")
		self.assertEqual(str(filed["date_of_joining"]), frappe.utils.today())
		# The date it replaced is reported rather than lost — see the wrapper.
		self.assertIn(
			{"field": "status", "from": "Left", "to": "Active"},
			[dict(entry) for entry in row["changed"]],
		)

	# ── v0.46.2: the branch a returning seasonal worker takes ───────────────
	def the_compliance_columns(self):
		"""Employee as `compliance_fields.py` leaves a migrated site.

		Built from that module's own specs rather than restated, for the reason
		`test_farmops_api.py` gives: a flag or an option changed there has to be
		able to fail here."""
		from erpnext_mcp import compliance_fields

		from .harness import add_field

		for spec in compliance_fields.targets_by_doctype()["Employee"].fields:
			add_field(
				"Employee",
				spec.fieldname,
				fieldtype=spec.fieldtype,
				options=spec.options or None,
				label=spec.label,
				reqd=1 if spec.reqd else 0,
			)

	def the_i9_document_table(self):
		"""Four of the 24 `i9_documents.py` seeds, one per branch these tests take.

		SEEDED HERE RATHER THAN INSTALLED, because `install.py` runs against a
		bench and this suite does not. Four rather than 24 for the reason every
		fixture in this file is minimal: the tests below assert which list a
		document came back in, and 24 rows would make the assertion a transcription
		of `i9_documents.py` rather than a check of the grouping.
		"""
		STORE.seed(
			"I-9 Document Type",
			[
				{
					"name": "U.S. Passport",
					"doc_title": "U.S. Passport",
					"list_category": "A",
					"uscis_code": "passport",
					"requires_photo": 1,
					"enabled": 1,
					"description": "Unexpired U.S. passport",
				},
				{
					"name": "Employment Authorization Document (Form I-766)",
					"doc_title": "Employment Authorization Document (Form I-766)",
					"list_category": "A",
					"uscis_code": "i-766",
					"requires_photo": 1,
					"enabled": 1,
					"description": "Unexpired Employment Authorization Document",
				},
				{
					"name": "Driver's License",
					"doc_title": "Driver's License",
					"list_category": "B",
					"uscis_code": "drivers-license",
					"requires_photo": 1,
					"enabled": 1,
					"description": "State-issued driver's license",
				},
				{
					"name": "Social Security Card (Unrestricted)",
					"doc_title": "Social Security Card (Unrestricted)",
					"list_category": "C",
					"uscis_code": "ssn-card",
					"requires_photo": 0,
					"enabled": 1,
					"description": "Social Security Account Number card",
				},
			],
		)

	def an_unsigned_i9(self, name="I9-2026-CONTRACT"):
		"""A submitted I-9 whose Section 1 attestation box is empty.

		The state `i9_section_1_unsigned` fires on, which is a form filled in
		perfectly and signed by nobody — not a late form. Seeded rather than
		walked through the wizard because what is under test is the route from
		the ALERT to the pad, and `submit_i9_section_1` would fill the box.

		THE RULES ARE SEEDED HERE because the four missing-signature rules are
		RECORD-ONLY — authored in the declarative vocabulary with no `Rule`
		object in `alerts/rules.py` to fall back to — so a site that has not run
		the seeder has no `i9_section_1_unsigned` for the sweep to run at all.
		"""
		compliance_rules.seed_compliance_rules()
		self.the_compliance_columns()
		STORE.seed(
			"I-9 Form",
			[
				{
					"name": name,
					"employee": self.NEW_HIRE,
					"employee_name": "Ana Ruiz",
					"company": MAIN,
					"status": "Section 1 Complete",
					"hire_date": "2026-07-01",
					"legal_first_name": "Ana",
					"legal_last_name": "Ruiz",
					"citizenship_status": "US Citizen",
				}
			],
		)
		return name

	def a_documented_returning_picker(self, i9_status="Pending", w4_status="Missing"):
		"""Somebody who worked last season: an I-9 verified, a W-4 filed, a badge.

		THE COLUMNS ARE LEFT WHERE `create_employee` PUT THEM, because that is
		where a real site's are: nothing in this app has ever moved them. The
		records are Complete and Active, which is the disagreement the endpoint
		exists to resolve."""
		self.the_compliance_columns()
		STORE.seed(
			"Employee",
			[
				{
					"name": "EMP-RETURNED",
					"employee_name": "Rosa Aguilar",
					"first_name": "Rosa",
					"last_name": "Aguilar",
					"company": MAIN,
					"status": "Left",
					"gender": "Female",
					"date_of_birth": "1991-04-18",
					"date_of_joining": "2025-06-02",
					"employment_type": "Seasonal Worker",
					"i9_status": i9_status,
					"w4_status": w4_status,
					"jurisdiction": "OR",
				}
			],
		)
		STORE.seed(
			"I-9 Form",
			[
				{
					"name": "I9-2025-RETURNED",
					"employee": "EMP-RETURNED",
					"company": MAIN,
					"status": "Complete",
					"hire_date": "2025-06-02",
					"verification_date": "2025-06-03",
				}
			],
		)
		STORE.seed(
			"W-4 Form",
			[
				{
					"name": "W4-2025-RETURNED",
					"employee": "EMP-RETURNED",
					"company": MAIN,
					"status": "Active",
					"tax_year": "2025",
					"effective_date": "2025-06-02",
				}
			],
		)
		STORE.seed(
			"Bucket Log Badge Map",
			[
				{
					"name": "BADGE-0042",
					"badge_id": "BADGE-0042",
					"employee": "EMP-RETURNED",
					"company": MAIN,
					"active": 1,
				}
			],
		)

	def test_30_get_employee(self):
		"""The record the wizard skips steps on, and the reconciliation it needs.

		THE VALUES ARE ASSERTED, NOT JUST THE TYPES. `satisfiedSteps`
		(`OnboardingModels.swift:352`) marks the I-9, the W-4 and the badge done
		from these three strings and nothing else — a type is what crashes, a
		value is what quietly re-collects an I-9 from somebody who has one."""
		self.the_hr_furniture()
		self.a_documented_returning_picker()

		row = self.wire("get_employee", employee="EMP-RETURNED")
		EmployeeDetailModel.decode(row, "get_employee")

		self.assertEqual(row["name"], "EMP-RETURNED")
		self.assertEqual(row["employee_name"], "Rosa Aguilar")
		self.assertEqual(row["status"], "Left")
		self.assertEqual(row["jurisdiction"], "OR")
		self.assertEqual(row["badge_id"], "BADGE-0042")
		# The columns say Pending/Missing on the stored row; the records answered.
		self.assertEqual(row["i9_status"], "Verified")
		self.assertEqual(row["w4_status"], "On-File")
		self.assertEqual(row["i9_status_recorded"], "Pending")
		self.assertEqual(row["w4_status_recorded"], "Missing")
		self.assertEqual(sorted(row["reconciled"]), ["i9_status", "w4_status"])
		self.assertEqual(row["i9"]["name"], "I9-2025-RETURNED")
		self.assertEqual(row["w4"]["name"], "W4-2025-RETURNED")

	def test_30_an_expired_i9_is_never_written_over_by_an_old_complete_record(self):
		"""The one direction that must not be reconciled. §1324a wants a returning
		worker re-verified when their authorization lapsed, and the record that
		says Complete is the very form that expired."""
		self.the_hr_furniture()
		self.a_documented_returning_picker(i9_status="Expired", w4_status="Requires-Update")

		row = self.wire("get_employee", employee="EMP-RETURNED")
		EmployeeDetailModel.decode(row, "get_employee")
		self.assertEqual(row["i9_status"], "Expired")
		self.assertEqual(row["w4_status"], "Requires-Update")
		self.assertEqual(row["reconciled"], [])
		# The records are still reported — the disagreement is visible, not hidden.
		self.assertTrue(row["i9_on_file"])
		self.assertTrue(row["w4_on_file"])

	def test_30_a_worker_with_no_history_needs_every_step(self):
		"""The other common case, and the one the columns are already right about."""
		self.the_hr_furniture()
		self.the_compliance_columns()
		frappe.db.set_value("Employee", self.NEW_HIRE, "i9_status", "Pending")
		frappe.db.set_value("Employee", self.NEW_HIRE, "w4_status", "Missing")

		row = self.wire("get_employee", employee=self.NEW_HIRE)
		EmployeeDetailModel.decode(row, "get_employee")
		self.assertEqual(row["i9_status"], "Pending")
		self.assertEqual(row["w4_status"], "Missing")
		self.assertIsNone(row["badge_id"])
		self.assertIsNone(row["i9"])
		self.assertIsNone(row["w4"])
		self.assertEqual(row["reconciled"], [])

	# ── v0.47.0: the I-9's two missing calls ────────────────────────────────
	def test_31_list_i9_document_types(self):
		"""The list the Section 2 picker has been hardcoding, as records.

		THE THREE LIST KEYS ARE THE CONTRACT, not `documents`. Section 2 asks
		"one from List A" OR "one from B and one from C", so a picker draws three
		sections and needs three keys it can rely on being there — including an
		empty one, because a missing `list_b` is a crash and an empty `list_b` is
		a section with nothing in it. That is asserted below against a site that
		has only List A documents enabled.
		"""
		self.the_hr_furniture()
		self.the_i9_document_table()

		row = self.wire("list_i9_document_types")
		UndecodedResponseModel.decode(row, "list_i9_document_types")

		self.assertEqual(row["count"], 4)
		self.assertEqual(
			[d["doc_title"] for d in row["list_a"]],
			["Employment Authorization Document (Form I-766)", "U.S. Passport"],
		)
		self.assertEqual([d["doc_title"] for d in row["list_b"]], ["Driver's License"])
		self.assertEqual([d["doc_title"] for d in row["list_c"]], ["Social Security Card (Unrestricted)"])
		self.assertIsNone(row["list_category"])
		# What a picker cell renders. A title with no `requires_photo` beside it
		# is a row the foreman cannot tell a photo document from.
		passport = next(d for d in row["list_a"] if d["doc_title"] == "U.S. Passport")
		self.assertEqual(passport["uscis_code"], "passport")
		self.assertEqual(passport["requires_photo"], 1)

	def test_31_a_filtered_list_still_carries_all_three_keys(self):
		self.the_hr_furniture()
		self.the_i9_document_table()

		row = self.wire("list_i9_document_types", list_category="a")
		self.assertEqual(row["list_category"], "A")
		self.assertEqual(row["count"], 2)
		self.assertEqual(row["list_b"], [])
		self.assertEqual(row["list_c"], [])

	def test_31_a_list_that_is_not_a_list_is_refused(self):
		self.the_hr_furniture()
		self.the_i9_document_table()
		with self.assertRaises(Exception) as caught:
			self.wire("list_i9_document_types", list_category="D")
		self.assertIn("is not an I-9 list", str(caught.exception))

	def test_32_reverify_i9(self):
		"""Section 3, on the record `get_employee` reports as expired.

		THIS IS THE PAIR test_30 LEFT OPEN. That test asserts a returning picker's
		expired I-9 is reported as expired and never written over; this is the call
		the wizard makes about it, and until v0.47.0 there was not one.
		"""
		self.the_hr_furniture()
		self.the_i9_document_table()
		self.a_documented_returning_picker(i9_status="Expired")

		row = self.wire(
			"reverify_i9",
			employee="EMP-RETURNED",
			reason="Work Authorization Expired",
			document_title="Employment Authorization Document (Form I-766)",
			document_number="SRC1234567890",
			issuing_authority="USCIS",
			document_expiry="2027-06-01",
			verifier_name="Ana Ramos",
			verifier_title="Farm Manager",
		)
		UndecodedResponseModel.decode(row, "reverify_i9")

		self.assertEqual(row["name"], "I9-2025-RETURNED")
		self.assertEqual(row["status"], "Complete")
		self.assertEqual(row["reverification_count"], 1)
		self.assertEqual(row["work_authorization_expiry"], "2027-06-01")
		self.assertFalse(row["receipt_pending"])

		# It APPENDED. The 2025 verification is still what the form says was
		# examined on the day of hire — the claim the child table exists for.
		filed = next(r for r in STORE.rows("I-9 Form") if r["name"] == "I9-2025-RETURNED")
		self.assertEqual(filed["verification_date"], "2025-06-03")
		self.assertEqual(len(filed["reverifications"]), 1)

		# THE ROUND TRIP, and the reason the whole feature exists. test_30 asserts
		# `get_employee` reports this picker's I-9 as Expired and refuses to
		# reconcile it away. After the reverification the same read has to stop
		# saying so, or the wizard sends a lawfully reverified worker to
		# `create_i9_form` — which refuses, because she has one.
		self.assertEqual(row["employee_i9_status"], "Verified")
		detail = self.wire("get_employee", employee="EMP-RETURNED")
		self.assertEqual(detail["i9_status"], "Verified")

	def test_32_a_reverification_of_a_form_with_no_section_2_is_refused(self):
		self.the_hr_furniture()
		self.the_i9_document_table()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		with self.assertRaises(Exception) as caught:
			self.wire(
				"reverify_i9",
				employee=self.NEW_HIRE,
				reason="Work Authorization Expired",
				document_title="U.S. Passport",
				verifier_name="Ana Ramos",
			)
		self.assertIn("needs a first one to follow", str(caught.exception))

	# ── v0.47.1: the I-9 as a document ──────────────────────────────────────
	def test_33_get_i9_form(self):
		"""The read the wizard never had, and the four questions an audit asks.

		EVERY OTHER I-9 CALL HANDS BACK THE RECORD IT JUST WROTE. A foreman who
		opens the flow on somebody already verified could be told `Verified` by
		`get_employee` and nothing else — which documents, examined by whom, is
		anything still owed. All of it was on the server and none of it was
		reachable.
		"""
		self.the_hr_furniture()
		self.the_i9_document_table()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		self.wire(
			"submit_i9_section_1",
			employee=self.NEW_HIRE,
			citizenship_status="US Citizen",
			ssn_last_four="6789",
			address_street="1420 Orchard Road",
			address_city="Yakima",
			address_state="WA",
			address_zip="98901",
			date_of_birth="1994-03-11",
		)
		self.wire(
			"submit_i9_section_2",
			employee=self.NEW_HIRE,
			document_path="List A",
			list_a_doc_type="U.S. Passport",
			list_a_authority="US Dept of State",
			list_a_doc_number="P1234567",
			verifier_name="Ana Ramos",
			verifier_title="Farm Manager",
			verification_date=frappe.utils.today(),
		)

		row = self.wire("get_i9_form", employee=self.NEW_HIRE)
		UndecodedResponseModel.decode(row, "get_i9_form")

		self.assertEqual(row["employee"], self.NEW_HIRE)
		# v0.64.2, and incidental to what this test is about: the wizard posts
		# neither attestation, so the form it builds rests here rather than at
		# Complete. See test_20 for the argument.
		self.assertEqual(row["status"], "Awaiting Verification")
		self.assertEqual(row["list_a_doc_title"], "U.S. Passport")
		self.assertEqual(row["verifier_name"], "Ana Ramos")
		self.assertEqual(row["address_city"], "Yakima")
		self.assertEqual(row["reverifications"], [])
		self.assertEqual(row["reverification_count"], 0)

	def test_33_the_ssn_comes_back_as_the_last_four_and_nothing_more(self):
		"""`_i9_fields` does not list `ssn_full` and argues why it never will."""
		self.the_hr_furniture()
		self.the_i9_document_table()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		self.wire(
			"submit_i9_section_1",
			employee=self.NEW_HIRE,
			citizenship_status="US Citizen",
			ssn_last_four="123-45-6789",
		)
		row = self.wire("get_i9_form", employee=self.NEW_HIRE)
		self.assertEqual(row["ssn_last_four"], "6789")
		self.assertNotIn("ssn_full", row)

	def test_33_a_worker_may_read_their_own_and_not_somebody_elses(self):
		"""The same one-record exception `get_employee` carries, and its limit.

		Ana is a Field Worker here — `the_hr_furniture` is deliberately NOT
		called — so the HR role is absent, which is the state a picker's phone is
		actually in.
		"""
		install_hrms()
		STORE.seed(
			"Employee",
			[
				{
					"name": "EMP-ANA",
					"employee_name": "Ana Ramos",
					"user_id": WORKER,
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": "EMP-OTHER",
					"employee_name": "Marco Vega",
					"company": MAIN,
					"status": "Active",
				},
			],
		)
		STORE.seed(
			"I-9 Form",
			[
				{
					"name": "I9-2026-ANA",
					"employee": "EMP-ANA",
					"company": MAIN,
					"status": "Complete",
					"hire_date": "2026-04-01",
				},
				{
					"name": "I9-2026-OTHER",
					"employee": "EMP-OTHER",
					"company": MAIN,
					"status": "Complete",
					"hire_date": "2026-04-01",
				},
			],
		)
		self.be()

		mine = self.wire("get_i9_form", employee="EMP-ANA")
		self.assertEqual(mine["name"], "I9-2026-ANA")

		with self.assertRaises(Exception):
			self.wire("get_i9_form", employee="EMP-OTHER")

	@unittest.skipUnless(
		i9_pdf.available(),
		"filling the federal form needs pypdf and the shipped USCIS template; this bench "
		"has one or neither, which is what render_i9_pdf goes unavailable for.",
	)
	def test_34_generate_i9_pdf(self):
		"""The end of the onboarding flow: a URL the phone can print from."""
		self.the_hr_furniture()
		self.the_i9_document_table()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		self.wire(
			"submit_i9_section_1",
			employee=self.NEW_HIRE,
			citizenship_status="US Citizen",
			ssn_last_four="6789",
			date_of_birth="1994-03-11",
			address_street="1420 Orchard Road",
		)
		self.wire(
			"submit_i9_section_2",
			employee=self.NEW_HIRE,
			document_path="List A",
			list_a_doc_type="U.S. Passport",
			list_a_authority="US Dept of State",
			list_a_doc_number="P1234567",
			verifier_name="Ana Ramos",
			verification_date=frappe.utils.today(),
		)

		row = self.wire("generate_i9_pdf", employee=self.NEW_HIRE)
		UndecodedResponseModel.decode(row, "generate_i9_pdf")

		self.assertTrue(str(row["file_url"]).endswith(".pdf"))
		self.assertGreater(row["bytes"], 100_000)
		self.assertIn("1615-0047", row["edition"])
		self.assertEqual(row["incomplete"], [])
		self.assertEqual(row["reverifications_not_on_page"], 0)
		self.assertIn("8 CFR 274a.2(h)", row["note"])

		# The URL the phone was handed is the one on the record.
		name = frappe.db.get_value("I-9 Form", {"employee": self.NEW_HIRE}, "name")
		self.assertEqual(frappe.db.get_value("I-9 Form", name, "generated_pdf"), row["file_url"])

	def test_34_the_full_ssn_is_not_an_argument_a_handset_has(self):
		"""It would print a nine-digit number onto a page a phone could mail
		anywhere, and it needs a retention decision an operator makes.

		AND FROM v0.137.0 THERE IS A SECOND REASON, which is the architecture
		rather than the policy: the handset BUILDS the retained I-9 itself, where
		the nine digits already are, and files the finished page with
		`upload_signed_i9`. This endpoint draws the working copy. v0.136.0 briefly
		accepted the flag here and it was reverted.
		"""
		import inspect

		self.assertNotIn("include_full_ssn", inspect.signature(mobile_api.generate_i9_pdf).parameters)

	def test_35_upload_signed_i9(self):
		"""The photograph of the signed sheet, filed against the record.

		THE UPLOAD IS THE EVIDENCE PATH, unchanged: `stage_file_chunk` then
		`finalize_staged_file`, and the token that returns is what this call
		names. No bytes cross this endpoint.
		"""
		self.the_hr_furniture()
		self.the_i9_document_table()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		_staged, finalized = self.upload(kind="signed-i9", name="signed-i9.pdf")

		row = self.wire("upload_signed_i9", employee=self.NEW_HIRE, file_token=finalized["file_token"])
		SignedI9Model.decode(row, "upload_signed_i9")

		self.assertEqual(row["file_token"], finalized["file_token"])
		self.assertTrue(row["signed_pdf"])
		self.assertIsNone(row["replaced"])

		name = frappe.db.get_value("I-9 Form", {"employee": self.NEW_HIRE}, "name")
		self.assertEqual(frappe.db.get_value("I-9 Form", name, "signed_pdf"), row["signed_pdf"])
		self.assertEqual(int(frappe.db.get_value("File", finalized["file_token"], "is_private") or 0), 1)

	def test_35_a_call_with_no_token_says_what_to_upload_first(self):
		self.the_hr_furniture()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		with self.assertRaises(Exception) as caught:
			self.wire("upload_signed_i9", employee=self.NEW_HIRE)
		self.assertIn("finalize_staged_file", str(caught.exception))

	# ── v0.47.2: what v0.47.0 taught the tool and the transport dropped ──────
	def test_36_the_i94_admission_number_reaches_the_record(self):
		"""v0.47.0 gave Section 1 three ways to identify an alien authorized to
		work and this wrapper carried one of them. A worker holding an I-94 and
		no A-Number filled the form on a phone and was refused for a field the
		transport had thrown away before the tool ever saw it."""
		self.the_hr_furniture()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		row = self.wire(
			"submit_i9_section_1",
			employee=self.NEW_HIRE,
			citizenship_status="Alien Authorized to Work",
			i94_admission_number="94123456789",
			work_authorization_expiry="2027-05-01",
		)
		self.assertEqual(row["status"], "Section 1 Complete")
		filed = next(iter(STORE.rows("I-9 Form")))
		self.assertEqual(filed["i94_admission_number"], "94123456789")
		self.assertEqual(str(filed["alien_work_authorization_expiry"]), "2027-05-01")

	def test_36_the_foreign_passport_pair_reaches_the_record_together(self):
		"""Both halves or neither. The tool refuses a passport number with no
		issuing country, and that refusal is only reachable if BOTH keys survive
		the transport — a wrapper carrying the number alone would turn a complete
		form into a permanent refusal."""
		self.the_hr_furniture()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		self.wire(
			"submit_i9_section_1",
			employee=self.NEW_HIRE,
			citizenship_status="Alien Authorized to Work",
			foreign_passport_number="X1234567",
			foreign_passport_country="Mexico",
			work_authorization_expiry="2027-05-01",
		)
		filed = next(iter(STORE.rows("I-9 Form")))
		self.assertEqual(filed["foreign_passport_number"], "X1234567")
		self.assertEqual(filed["foreign_passport_country"], "Mexico")

	def test_36_a_passport_with_no_country_is_still_the_tools_refusal(self):
		"""The point of forwarding the pair is that the tool gets to judge it."""
		self.the_hr_furniture()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		with self.assertRaises(Exception) as caught:
			self.wire(
				"submit_i9_section_1",
				employee=self.NEW_HIRE,
				citizenship_status="Alien Authorized to Work",
				foreign_passport_number="X1234567",
			)
		self.assertIn("foreign_passport_country", str(caught.exception))

	def test_37_a_receipt_examined_on_a_phone_is_recorded_as_a_receipt(self):
		"""THE ONE THAT MATTERS. Dropping the flag did not lose a nicety: it
		filed a receipt as though the document itself had been examined, which is
		a false attestation on a federal form, and the 90-day clock that says when
		the real document is owed never started."""
		self.the_hr_furniture()
		self.the_i9_document_table()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		self.wire("submit_i9_section_1", employee=self.NEW_HIRE, citizenship_status="US Citizen")
		row = self.wire(
			"submit_i9_section_2",
			employee=self.NEW_HIRE,
			document_path="List A",
			list_a_doc_type="U.S. Passport",
			list_a_authority="US Dept of State",
			list_a_doc_number="P1234567",
			list_a_is_receipt=True,
			verifier_name="Ana Ramos",
			verifier_title="Farm Manager",
			verification_date=frappe.utils.today(),
		)
		# THE RECEIPT IS STILL NOT WHAT HOLDS THIS FORM OPEN, which is the claim
		# this test protects: under 8 CFR 274a.2(b)(1)(vi) the worker may work
		# while the replacement comes, so `list_a_is_receipt` does not block
		# completion. What holds it open is the missing attestation, and the two
		# reasons must not be confused — `unsigned` names the one in force.
		self.assertEqual(row["status"], "Awaiting Verification")
		self.assertEqual(
			row["unsigned"],
			["Section 1 (the employee's attestation)", "Section 2 (the employer's attestation)"],
		)
		filed = next(iter(STORE.rows("I-9 Form")))
		self.assertTrue(int(filed["list_a_is_receipt"] or 0))
		self.assertTrue(int(filed["receipt_pending"] or 0))
		self.assertTrue(filed["receipt_expires_on"])

	def test_37_the_b_and_c_receipt_flags_travel_independently(self):
		"""Two documents, two answers. A worker may present the real driver's
		licence and a receipt for the replacement Social Security card, and a
		transport that carried one flag for both would record the wrong one."""
		self.the_hr_furniture()
		self.the_i9_document_table()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		self.wire("submit_i9_section_1", employee=self.NEW_HIRE, citizenship_status="US Citizen")
		self.wire(
			"submit_i9_section_2",
			employee=self.NEW_HIRE,
			document_path="List B + C",
			list_b_doc_type="Driver's License",
			list_b_authority="Oregon DMV",
			list_b_is_receipt=False,
			list_c_doc_type="Social Security Card (Unrestricted)",
			list_c_authority="SSA",
			list_c_is_receipt=True,
			verifier_name="Ana Ramos",
			verifier_title="Farm Manager",
			verification_date=frappe.utils.today(),
		)
		filed = next(iter(STORE.rows("I-9 Form")))
		self.assertFalse(int(filed["list_b_is_receipt"] or 0))
		self.assertTrue(int(filed["list_c_is_receipt"] or 0))

	def test_37_a_section_2_that_sends_no_flag_records_no_receipt(self):
		"""The pre-v0.47.2 caller, unchanged. An app that has not grown the
		checkbox sends nothing and gets the honest answer rather than a default
		that quietly asserts a receipt nobody saw."""
		self.the_hr_furniture()
		self.the_i9_document_table()
		self.wire("create_i9_form", employee=self.NEW_HIRE, company=MAIN, hire_date=frappe.utils.today())
		self.wire("submit_i9_section_1", employee=self.NEW_HIRE, citizenship_status="US Citizen")
		self.wire(
			"submit_i9_section_2",
			employee=self.NEW_HIRE,
			document_path="List A",
			list_a_doc_type="U.S. Passport",
			list_a_authority="US Dept of State",
			verifier_name="Ana Ramos",
			verification_date=frappe.utils.today(),
		)
		filed = next(iter(STORE.rows("I-9 Form")))
		self.assertFalse(int(filed["list_a_is_receipt"] or 0))
		self.assertFalse(int(filed["receipt_pending"] or 0))

	# ── v0.48.0: who may sign, and the W-4 as a document ─────────────────────
	def test_38_list_authorized_signers(self):
		"""The read the Section 2 screen needs BEFORE it offers a name.

		`configured` IS THE KEY THE APP BRANCHES ON. False means this site has no
		roster and the free-text verifier box is still correct; true means the
		name is the server's to supply. A wizard that could not ask would have to
		learn its own account's authorisation by submitting a form in an orchard
		and being refused.
		"""
		self.the_hr_furniture()
		row = self.wire("list_authorized_signers")
		UndecodedResponseModel.decode(row, "list_authorized_signers")

		self.assertEqual(row["signers"], [])
		self.assertEqual(row["count"], 0)
		self.assertFalse(row["configured"])
		self.assertEqual(row["active_count"], 0)
		self.assertIn("UNRESTRICTED", row["note"])

	def test_38_the_roster_reads_back_the_row_the_phone_added(self):
		self.the_hr_furniture()
		added = self.wire(
			"add_authorized_signer",
			signer_user=WORKER,
			full_name="Ana Ramos",
			title="Farm Manager",
		)
		UndecodedResponseModel.decode(added, "add_authorized_signer")
		self.assertTrue(added["first_signer"])
		self.assertIn("FIRST signer", added["note"])

		row = self.wire("list_authorized_signers")
		self.assertTrue(row["configured"])
		self.assertEqual(row["signers"][0]["user"], WORKER)
		self.assertEqual(row["signers"][0]["full_name"], "Ana Ramos")
		self.assertTrue(row["signers"][0]["can_sign_i9"])
		self.assertTrue(row["signers"][0]["active"])

	def test_38_the_account_being_authorized_is_not_called_user(self):
		"""`signer_user`, and the rename is a gate rather than a preference.

		`guard.endpoint` injects the AUTHENTICATED caller into `user` and
		`routes.accepted_arguments` drops any `user` a body carries — so an
		argument called `user` would be dropped on the way in and the endpoint
		would silently authorise whoever called it.
		"""
		import inspect

		for method in (
			mobile_api.add_authorized_signer,
			mobile_api.update_authorized_signer,
			mobile_api.remove_authorized_signer,
		):
			with self.subTest(method=method.farm_ops_method):
				self.assertIn("signer_user", inspect.signature(method).parameters)

	def test_39_update_authorized_signer(self):
		self.the_hr_furniture()
		self.wire("add_authorized_signer", signer_user=WORKER, full_name="Ana Ramos")
		row = self.wire("update_authorized_signer", signer_user=WORKER, title="Orchard Manager")
		UndecodedResponseModel.decode(row, "update_authorized_signer")
		self.assertEqual(row["signer"]["title"], "Orchard Manager")
		self.assertEqual(row["updated"], ["title"])

	def test_40_remove_authorized_signer(self):
		"""Deactivation, and the warning that it left nobody able to sign."""
		self.the_hr_furniture()
		self.wire("add_authorized_signer", signer_user=WORKER, full_name="Ana Ramos")
		row = self.wire("remove_authorized_signer", signer_user=WORKER)
		UndecodedResponseModel.decode(row, "remove_authorized_signer")

		self.assertFalse(row["active"])
		self.assertFalse(row["deleted"])
		self.assertEqual(row["remaining_active"], 0)
		self.assertIn("NO ACTIVE SIGNERS REMAIN", row["warning"])
		# The row is still there. A form signed before the removal still names
		# somebody this employer had authorised.
		self.assertEqual(self.wire("list_authorized_signers")["count"], 1)

	@unittest.skipUnless(
		w4_pdf.available(),
		"filling the federal form needs pypdf and the shipped IRS template; this bench has "
		"one or neither, which is what render_w4_pdf goes unavailable for.",
	)
	def test_41_generate_w4_pdf(self):
		"""The W-4 as a page rather than a doctype, and no EIN typed on a phone."""
		self.the_hr_furniture()
		self.wire(
			"submit_w4",
			employee=self.NEW_HIRE,
			company=MAIN,
			tax_year=2026,
			filing_status="Married Filing Jointly",
			dependents_under_17=2,
		)

		row = self.wire("generate_w4_pdf", employee=self.NEW_HIRE)
		UndecodedResponseModel.decode(row, "generate_w4_pdf")

		self.assertTrue(str(row["file_url"]).endswith(".pdf"))
		self.assertGreater(row["bytes"], 100_000)
		self.assertIn("1545-0074", row["edition"])
		self.assertEqual(row["tax_year"], 2026)
		self.assertTrue(row["template_tax_year_matches"])
		self.assertIn("NO signature field", row["note"])

		# The URL the phone was handed is the one on the record.
		name = frappe.db.get_value("W-4 Form", {"employee": self.NEW_HIRE}, "name")
		self.assertEqual(frappe.db.get_value("W-4 Form", name, "generated_pdf"), row["file_url"])

	def test_41_the_employer_block_is_not_an_argument_a_handset_has(self):
		"""It would put a hand-typed EIN on a federal form. It is resolved server-side."""
		import inspect

		taken = set(inspect.signature(mobile_api.generate_w4_pdf).parameters)
		for absent in ("employer_name", "employer_address", "employer_ein", "first_date_of_employment"):
			self.assertNotIn(absent, taken)

	# ── v0.48.3: the onboarding evidence that was never being stored ─────────
	def test_42_attach_onboarding_document(self):
		"""The call the wizard's six photographs and signatures had no route for.

		THE DEFECT. `OnboardingAPI.attachDocument` POSTed multipart to Frappe's
		own `/api/method/upload_file`, which `fallback_auth._is_mobile_path` does
		not match, so the `X-FarmOps-Token` header was never read on it. Through
		the funnel — which strips `Authorization` — the request arrived as Guest
		and Frappe answered 200 with the Desk login page; the old `uploadFile`
		returned on any 2xx without looking at the body. Every I-9 document
		photo, both signatures and the photographed W-4 were reported filed and
		stored nowhere.

		The fix is this endpoint plus the staged path in front of it, and what
		this test proves is the part that matters legally: after the call there
		is a File, it is PRIVATE, and it is attached to the Employee.
		"""
		self.the_hr_furniture()
		_staged, finalized = self.upload(kind="list-b", name="i9_list_b_doc.jpg")

		row = self.wire(
			"attach_onboarding_document",
			employee=self.NEW_HIRE,
			file_token=finalized["file_token"],
			document_kind="i9_list_b_document",
		)
		AttachedDocumentModel.decode(row, "attach_onboarding_document")

		self.assertEqual(row["file_token"], finalized["file_token"])
		self.assertEqual(row["document_kind"], "i9_list_b_document")
		self.assertIs(row["already_attached"], False)

		filed = frappe.db.get_value(
			"File",
			finalized["file_token"],
			["attached_to_doctype", "attached_to_name", "is_private"],
			as_dict=True,
		)
		self.assertEqual(filed["attached_to_doctype"], "Employee")
		self.assertEqual(filed["attached_to_name"], self.NEW_HIRE)
		self.assertEqual(int(filed["is_private"] or 0), 1)

	def test_42_a_retried_attach_is_a_no_op_rather_than_a_second_copy(self):
		"""A phone whose call timed out does not know whether it landed, and the
		only safe answer is the one it would have got."""
		self.the_hr_furniture()
		_staged, finalized = self.upload(kind="sig", name="i9_signature.png")
		first = self.wire(
			"attach_onboarding_document",
			employee=self.NEW_HIRE,
			file_token=finalized["file_token"],
			document_kind="i9_section_1_signature",
		)
		second = self.wire(
			"attach_onboarding_document",
			employee=self.NEW_HIRE,
			file_token=finalized["file_token"],
			document_kind="i9_section_1_signature",
		)
		AttachedDocumentModel.decode(second, "attach_onboarding_document")
		self.assertIs(first["already_attached"], False)
		self.assertIs(second["already_attached"], True)
		self.assertEqual(first["file_token"], second["file_token"])

	def test_42_a_file_filed_against_somebody_else_is_refused(self):
		"""Re-pointing an attachment would take evidence off the record it was
		filed against, and both records are federal ones."""
		self.the_hr_furniture()
		_staged, finalized = self.upload(kind="passport", name="i9_list_a_doc.jpg")
		self.wire(
			"attach_onboarding_document",
			employee=self.NEW_HIRE,
			file_token=finalized["file_token"],
			document_kind="i9_list_a_document",
		)
		with self.assertRaises(Exception) as caught:
			self.wire(
				"attach_onboarding_document",
				employee=self.SECOND_HAND,
				file_token=finalized["file_token"],
				document_kind="i9_list_a_document",
			)
		self.assertIn("already attached", str(caught.exception))

	def test_42_a_call_with_no_token_says_what_to_upload_first(self):
		"""No bytes cross this endpoint, and the refusal has to say so — an app
		that could send a body here would be the second upload path this release
		exists to remove."""
		self.the_hr_furniture()
		with self.assertRaises(Exception) as caught:
			self.wire("attach_onboarding_document", employee=self.NEW_HIRE)
		self.assertIn("finalize_staged_file", str(caught.exception))

	def test_42_no_bytes_are_an_argument_this_endpoint_takes(self):
		"""The property that keeps there being ONE upload path. A base64 body here
		would have its own size limit and its own way of failing halfway up a
		hill, which is the argument `api/files.py` has made since v0.14.0."""
		import inspect

		taken = set(inspect.signature(mobile_api.attach_onboarding_document).parameters)
		for absent in ("data", "file_content", "content_base64", "image", "is_private"):
			self.assertNotIn(absent, taken)

	# ── v0.51.0: issuing a badge, and the face that goes on it ──────────────

	def test_44_set_employee_photo(self):
		"""`EmployeePhoto` — the headshot, and the field update that is the point.

		THE DEFECT THIS CLOSES. `attach_onboarding_document` files evidence: the
		bytes land as a private File pointing at the Employee and NOTHING on the
		Employee points back. `generate_employee_badge_sheet` lays a card out
		from `Employee.image` and falls back to initials where it is empty — so
		every badge printed for somebody onboarded through the wizard showed two
		letters where a face should be. This is the same staged upload followed
		by the one field write that closes the loop, and what the test proves is
		that write.
		"""
		self.the_hr_furniture()
		_staged, finalized = self.upload(kind="profile-photo", name="employee_photo.jpg")

		row = self.wire(
			"set_employee_photo",
			employee=self.NEW_HIRE,
			file_token=finalized["file_token"],
		)
		EmployeePhotoModel.decode(row, "set_employee_photo")

		self.assertIs(row["image_set"], True)
		self.assertTrue(row["photo_url"])
		# The whole point: the Employee now points AT the file.
		self.assertEqual(frappe.db.get_value("Employee", self.NEW_HIRE, "image"), row["photo_url"])
		# And it is still private — a workforce's faces on a guessable public
		# URL is a breach nobody needs a password for.
		filed = frappe.db.get_value(
			"File",
			finalized["file_token"],
			["attached_to_doctype", "attached_to_name", "is_private"],
			as_dict=True,
		)
		self.assertEqual(filed["attached_to_doctype"], "Employee")
		self.assertEqual(filed["attached_to_name"], self.NEW_HIRE)
		self.assertEqual(int(filed["is_private"] or 0), 1)

	def test_44_a_retaken_photo_supersedes_the_previous_one(self):
		"""A retake moves `image` and leaves the old file attached. The record
		keeps what it was shown; the card prints what is current."""
		self.the_hr_furniture()
		# Distinct names because this double derives `file_url` from the name
		# alone. Frappe deduplicates a repeated upload by suffixing the URL, so
		# two retakes really do land on two URLs on a live site; naming them the
		# same here would test the double's shortcut rather than the retake.
		_s1, first = self.upload(kind="profile-photo", name="employee_photo.jpg")
		_s2, second = self.upload(kind="profile-photo", name="employee_photo_2.jpg")

		one = self.wire("set_employee_photo", employee=self.NEW_HIRE, file_token=first["file_token"])
		two = self.wire("set_employee_photo", employee=self.NEW_HIRE, file_token=second["file_token"])
		EmployeePhotoModel.decode(two, "set_employee_photo")

		self.assertIs(one["replaced"], False)
		self.assertIs(two["replaced"], True)
		self.assertEqual(frappe.db.get_value("Employee", self.NEW_HIRE, "image"), two["photo_url"])
		self.assertNotEqual(one["photo_url"], two["photo_url"])

	def test_44_a_pdf_is_not_a_face(self):
		"""`Employee.image` is rendered in an `<img>` by Desk, by the badge
		template and by the phone. A PDF is a fine scan of a signed W-4 and is
		refused here BEFORE the attach, so a mistake does not leave a file filed
		against the record with the field unset."""
		self.the_hr_furniture()
		_staged, finalized = self.upload(kind="profile-photo", name="scan.pdf")
		with self.assertRaises(Exception) as caught:
			self.wire("set_employee_photo", employee=self.NEW_HIRE, file_token=finalized["file_token"])
		self.assertIn("photograph is an image", str(caught.exception))
		self.assertFalse(frappe.db.get_value("Employee", self.NEW_HIRE, "image"))

	def test_44_a_call_with_no_token_says_what_to_upload_first(self):
		self.the_hr_furniture()
		with self.assertRaises(Exception) as caught:
			self.wire("set_employee_photo", employee=self.NEW_HIRE)
		self.assertIn("finalize_staged_file", str(caught.exception))

	def test_45_generate_employee_badge_qr(self):
		"""`IssuedBadge` — the card the wizard could not produce.

		THE DEFECT. `generate_employee_badge_qr` has minted readable `CF-0001`
		identifiers since v0.50.0 and was published on the MCP tool registry
		ONLY, which a handset does not speak. So the badge step could map a card
		somebody had printed elsewhere and could not issue one — on a hire day,
		in a yard, to a worker standing there waiting to be told their number.

		The identifier is the server's to mint and the shape is the contract:
		the abbreviation and a sequence, readable off a scuffed card.
		"""
		self.the_hr_furniture()
		row = self.wire("generate_employee_badge_qr", employee=self.NEW_HIRE, company=MAIN)
		IssuedBadgeModel.decode(row, "generate_employee_badge_qr")

		self.assertEqual(row["badge_id"], f"{MAIN_ABBR}-0001")
		self.assertEqual(row["employee"], self.NEW_HIRE)
		self.assertIs(row["created"], True)
		self.assertIs(row["reused"], False)
		self.assertEqual(row["retired_badges"], [])
		# The QR is drawn, and it is what the screen shows the worker.
		self.assertTrue(row["png_base64"])
		base64.b64decode(row["png_base64"])
		# Initials stand in until a headshot is filed — both are always sent so
		# a card is laid out from one answer.
		self.assertTrue(row["photo_placeholder"])

	def test_45_pressing_generate_twice_reprints_rather_than_issuing_a_second(self):
		"""The property that makes the wizard's button safe on a bad connection.

		A phone in a yard that never hears the answer presses again, and a call
		that minted a second identifier each time would put two live cards in
		one picker's pocket — which is a lost card that still resolves, paying
		one person's piecework to whoever found it.
		"""
		self.the_hr_furniture()
		first = self.wire("generate_employee_badge_qr", employee=self.NEW_HIRE, company=MAIN)
		second = self.wire("generate_employee_badge_qr", employee=self.NEW_HIRE, company=MAIN)
		IssuedBadgeModel.decode(second, "generate_employee_badge_qr")

		self.assertEqual(first["badge_id"], second["badge_id"])
		self.assertIs(second["created"], False)
		self.assertIs(second["reused"], True)

	def test_45_the_handset_cannot_name_the_identifier(self):
		"""`badge_id` is not a body key, and that is deliberate. The tool lets a
		Desk operator adopt a card from the old `farm_app` uuid stock; letting a
		handset do it would put the uniqueness of a payroll key in the hands of
		whatever a foreman typed into a box."""
		import inspect

		taken = set(inspect.signature(mobile_api.generate_employee_badge_qr).parameters)
		self.assertNotIn("badge_id", taken)
		# The lost-card path IS the phone's, because that is a field problem.
		self.assertIn("regenerate", taken)

	def test_45_the_card_carries_the_company_logo_when_the_site_has_one(self):
		"""`company_logo_url` — v0.51.0's Company field. A print template should
		not have to ask this app a second question to lay out a card, which is
		the same reason the photo and the initials are both on the wire."""
		self.the_hr_furniture()
		badges_tool.install_badge_logo_field()
		frappe.db.set_value("Company", MAIN, "badge_logo", "/files/farm_mark.png")

		row = self.wire("generate_employee_badge_qr", employee=self.NEW_HIRE, company=MAIN)
		IssuedBadgeModel.decode(row, "generate_employee_badge_qr")
		self.assertEqual(row["company_logo_url"], "/files/farm_mark.png")

	def test_45_a_site_with_no_logo_still_gets_a_badge(self):
		"""The field arrives on the next migrate and a card is perfectly legible
		without a mark on it. A badge nobody can print because a logo is missing
		would be a worse outcome by a wide margin."""
		self.the_hr_furniture()
		row = self.wire("generate_employee_badge_qr", employee=self.NEW_HIRE, company=MAIN)
		IssuedBadgeModel.decode(row, "generate_employee_badge_qr")
		self.assertIsNone(row["company_logo_url"])
		self.assertTrue(row["badge_id"])

	# ── v0.57.0: the compliance tab becomes somewhere work is finished ──────
	def a_dismissible_alert(self) -> str:
		"""One alert on the calendar, marked closable from the field.

		THE SWEEP IS AN OPERATOR'S CALL AND THE TICK IS SOMEBODY ELSE'S. Both are
		done as Administrator here for the same reason `test_09` runs the sweep
		that way: neither is a thing a phone does, and a fixture that let the
		worker do them would be testing a site nobody runs.
		"""
		frappe.local.session.user = "Administrator"
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})
		names = [row["name"] for row in STORE.rows("Compliance Alert")]
		self.assertTrue(names, "the sweep raised nothing for this fixture to dismiss")
		frappe.db.set_value("Compliance Alert", names[0], "can_dismiss", 1)
		self.be()
		return names[0]

	def test_46_dismiss_compliance_alert(self):
		"""§8.2 — the row a phone may close, and the reason that is the record.

		THE DEFAULT IS THE DESIGN AND IT IS ASSERTED FIRST. Every alert the sweep
		raises arrives closable by nobody; the button appears only where somebody
		with the whole picture said this particular finding is stale. A handset
		that could wave off an overdue housing inspection would leave a cabin
		uninspected and the calendar quiet about it, which is why v1 shipped with
		no dismiss at all.
		"""
		alert = self.a_dismissible_alert()

		listed = self.wire("list_compliance_alerts")
		by_name = {row["name"]: row for row in listed["alerts"]}
		for index, row in enumerate(listed["alerts"]):
			ComplianceAlertSummaryModel.decode(row, "list_compliance_alerts", f".alerts[{index}]")
		self.assertIs(by_name[alert]["can_dismiss"], True)
		self.assertTrue(
			[row for row in listed["alerts"] if row["can_dismiss"] is False],
			"every other alert should still be closable by nobody",
		)

		row = self.wire(
			"dismiss_compliance_alert", alert=alert, reason="Raised against a lease we ended in May."
		)
		VoidResponseModel.decode(row, "dismiss_compliance_alert")
		self.assertIs(row["dismissed"], True)
		self.assertEqual(row["reason"], "Raised against a lease we ended in May.")
		# The whole audit trail: who, when, why — on the alert, not in a log line
		# somebody has to correlate.
		stored = frappe.db.get_value(
			"Compliance Alert",
			alert,
			["dismissed", "auto_dismissed", "dismissed_by", "dismissed_reason"],
			as_dict=True,
		)
		self.assertEqual(int(stored["dismissed"] or 0), 1)
		self.assertEqual(int(stored["auto_dismissed"] or 0), 0)
		self.assertEqual(stored["dismissed_by"], WORKER)
		self.assertEqual(stored["dismissed_reason"], "Raised against a lease we ended in May.")
		# And it is off the calendar the phone reads.
		self.assertNotIn(alert, {row["name"] for row in self.wire("list_compliance_alerts")["alerts"]})

	def test_46_an_alert_nobody_marked_dismissible_is_refused(self):
		"""The gate is here and not only on the handset. The app hides the button
		on `can_dismiss: false` and its own contract calls that UI courtesy; a
		refusal that exists only in a client is not one."""
		frappe.local.session.user = "Administrator"
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})
		self.be()
		alert = self.wire("list_compliance_alerts")["alerts"][0]
		self.assertIs(alert["can_dismiss"], False)

		with self.assertRaises(Exception) as caught:
			self.wire("dismiss_compliance_alert", alert=alert["name"], reason="Not worth doing this week.")
		self.assertIn("not marked as dismissible", str(caught.exception))
		self.assertEqual(int(frappe.db.get_value("Compliance Alert", alert["name"], "dismissed") or 0), 0)

	def test_46_an_empty_reason_is_refused_before_anything_is_written(self):
		alert = self.a_dismissible_alert()
		with self.assertRaises(Exception) as caught:
			self.wire("dismiss_compliance_alert", alert=alert, reason="   ")
		self.assertIn("reason is required", str(caught.exception))
		self.assertEqual(int(frappe.db.get_value("Compliance Alert", alert, "dismissed") or 0), 0)

	def test_47_submit_form_signature(self):
		"""§14.2/§14.3 — the pad's own call, addressed off the alert.

		THE ROUTE NAME AND THE ARGUMENT NAMES ARE THE POINT. v0.55.0 published
		this write as `collect_signature`, which declares `field` and
		`signature_base64`; the app posts `signature_field` and
		`signature_image`, and `routes.bind` drops what a signature does not
		name — so the contract's own body reached the old method with neither the
		field nor the picture in it.
		"""
		self.the_hr_furniture()
		self.an_unsigned_i9()
		frappe.local.session.user = "Administrator"
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})
		self.be()

		alert = next(
			row for row in self.wire("list_compliance_alerts")["alerts"] if row.get("signature_request")
		)
		ComplianceAlertSummaryModel.decode(alert, "list_compliance_alerts")
		request = alert["signature_request"]
		# The three addressing keys are what make the row a tap rather than a
		# sentence. `isActionable` is false without all three.
		self.assertEqual(request["doctype"], "I-9 Form")
		self.assertEqual(request["docname"], "I9-2026-CONTRACT")
		self.assertEqual(request["signature_field"], "section_1_signature")

		row = self.wire(
			"submit_form_signature",
			doctype=request["doctype"],
			docname=request["docname"],
			signature_field=request["signature_field"],
			signature_image=A_PNG,
			signer_role=request.get("signer_role"),
			printed_name=request.get("printed_name"),
			task=alert.get("linked_task"),
		)
		SignatureSubmissionModel.decode(row, "submit_form_signature")

		self.assertEqual(row["field"], "section_1_signature")
		self.assertTrue(row["file_url"])
		self.assertIs(row["already_signed"], False)
		# The alert the phone tapped is named back, so the row it opened comes off
		# the tab it was opened from.
		self.assertEqual(row["dismissed_alert"], alert["name"])
		self.assertTrue(frappe.db.get_value("I-9 Form", "I9-2026-CONTRACT", "section_1_signature"))

	@unittest.skipUnless(
		i9_pdf.available(),
		"the rendered page needs pypdf and the shipped USCIS template; this bench "
		"has one or neither, which is what render_i9_pdf goes unavailable for.",
	)
	def test_47_the_signed_page_comes_back_in_the_answer(self):
		"""v0.57.1. The artefact the whole flow exists to produce, in the hands of
		the person who just signed it.

		`file_url` NAMES A FILE THIS CALLER CANNOT FETCH. It is a private File on
		the site and the app authenticates to the sidecar with `X-FarmOps-Token`
		rather than to Frappe — §14.3 has said "recorded, not displayed" about it
		since it was written. So the page travels as base64, the way
		`get_employee_badge_pass` sends a `.pkpass`, and `render_i9_pdf` stamps
		the capture into the page content, which is what makes it the SIGNED form
		rather than a blank-box copy of the same record.
		"""
		self.the_hr_furniture()
		self.an_unsigned_i9()
		row = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=A_PNG,
		)
		SignatureSubmissionModel.decode(row, "submit_form_signature")

		pdf = row["pdf"]
		self.assertIs(pdf["available"], True)
		self.assertIs(pdf["regenerated"], True)
		self.assertEqual(pdf["content_type"], "application/pdf")
		self.assertTrue(pdf["file_url"])
		# A real PDF, not a note about one. The magic bytes are the assertion
		# that matters: a base64 string that decodes to anything else would pass
		# every other check here and put an empty viewer in front of a foreman.
		self.assertTrue(base64.b64decode(pdf["base64"]).startswith(b"%PDF"))
		self.assertEqual(pdf["bytes"], len(base64.b64decode(pdf["base64"])))
		# And it is filed against the record, not only handed down the wire.
		self.assertEqual(
			frappe.db.get_value("I-9 Form", "I9-2026-CONTRACT", "generated_pdf"), pdf["file_url"]
		)

	@unittest.skipUnless(
		i9_pdf.available(),
		"reading the page back needs pypdf and the shipped USCIS template; this bench "
		"has one or neither, which is what render_i9_pdf goes unavailable for.",
	)
	def test_47_the_page_that_comes_back_carries_the_ink(self):
		"""THE PAGE IS ONLY WORTH SENDING IF THE SIGNATURE IS ON IT, and from
		v0.51.0 to v0.57.0 it was not — on any site, for any form.

		`i9_pdf.py` says at length that the capture is stamped into the page
		content and the result flattened, so the retained copy cannot be edited
		back into an unsigned one. It was true of `fill_i9_pdf`, which is where
		the stamping tests point, and false of everything that CALLED it:
		`render_i9_pdf` built its record from `_i9_fields()`, which does not
		carry `section_1_signature` or `section_2_signature` — those are the URLs
		of the ink, and a reader of the record has no use for them — so
		`_signature_captures` looked for two keys that were never in the dict and
		returned `{}` every time. No captures meant no stamping AND no
		flattening, which is why the field count below is the assertion: an
		unflattened page is the tell, and it is visible without decoding an
		image.

		A REAL PNG rather than the eight magic bytes the tests above use. The
		renderer decodes this one, and a fixture that only looks like a PNG would
		pass through the same silent skip this test exists to catch.
		"""
		self.the_hr_furniture()
		self.an_unsigned_i9()
		row = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=base64.b64encode(A_REAL_PNG).decode(),
		)
		from pypdf import PdfReader

		page = base64.b64decode(row["pdf"]["base64"])
		reader = PdfReader(io.BytesIO(page))
		self.assertEqual(
			len(reader.get_fields() or {}),
			0,
			"the page came back unflattened, which means no capture reached the renderer — "
			"the signature is on the record and invisible on the copy of it a worker is shown.",
		)
		# And the electronic-signature record beside the image, which is what
		# makes it an attestation under 8 CFR 274a.2(h) rather than a picture.
		# Matched without the trailing word, because the renderer wraps the
		# sentence into the Additional Information box and the line break lands
		# in a different place for a different name.
		text = "\n".join((sheet.extract_text() or "") for sheet in reader.pages)
		self.assertIn("signature image affixed", text)
		self.assertIn("Electronically signed pursuant to 8 CFR 274a.2", text)

	def test_47_a_client_that_does_not_want_the_page_is_not_sent_one(self):
		"""`include_pdf=false` for a caller that only wants the write. Drawing a
		federal form is not free and a client that will not show it should not
		make the phone wait for it."""
		self.the_hr_furniture()
		self.an_unsigned_i9()
		row = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=A_PNG,
			include_pdf=False,
		)
		SignatureSubmissionModel.decode(row, "submit_form_signature")
		self.assertIsNone(row["pdf"])
		# The signature is on the record regardless — the page was the only
		# thing turned off.
		self.assertTrue(frappe.db.get_value("I-9 Form", "I9-2026-CONTRACT", "section_1_signature"))
		self.assertFalse(frappe.db.get_value("I-9 Form", "I9-2026-CONTRACT", "generated_pdf") or "")

	def test_47_a_page_that_cannot_be_drawn_is_reported_and_is_not_fatal(self):
		"""NEVER FATAL. A site without `pypdf` or the blank federal form has a
		working signature and no page, and the phone is told which — a call that
		threw away the attestation to avoid reporting the loss of a picture of it
		would have the trade backwards."""
		self.the_hr_furniture()
		self.an_unsigned_i9()

		def no_renderer(_args):
			raise RuntimeError("pypdf is not installed on this site")

		original = i9_tools.render_i9_pdf
		i9_tools.render_i9_pdf = no_renderer
		self.addCleanup(setattr, i9_tools, "render_i9_pdf", original)

		row = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=A_PNG,
		)
		SignatureSubmissionModel.decode(row, "submit_form_signature")
		self.assertIs(row["pdf"]["available"], False)
		self.assertIsNone(row["pdf"]["base64"])
		self.assertIn("pypdf", row["pdf"]["note"])
		# THE SIGNATURE LANDED. That is the whole point of the assertion above.
		self.assertIs(row["already_signed"], False)
		self.assertTrue(row["file_url"])
		self.assertTrue(frappe.db.get_value("I-9 Form", "I9-2026-CONTRACT", "section_1_signature"))

	@unittest.skipUnless(
		i9_pdf.available(),
		"the rendered page needs pypdf and the shipped USCIS template; this bench "
		"has one or neither, which is what render_i9_pdf goes unavailable for.",
	)
	def test_47_a_retry_gets_the_page_back_without_drawing_a_second_one(self):
		"""§14.4's idempotent path stays a READ. The worker is standing there and
		wants the same page; rendering one here would make the branch that exists
		not to write, write."""
		self.the_hr_furniture()
		self.an_unsigned_i9()
		first = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=A_PNG,
		)
		second = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=A_PNG,
		)
		SignatureSubmissionModel.decode(second, "submit_form_signature")
		self.assertIs(second["already_signed"], True)
		self.assertIs(second["pdf"]["available"], True)
		self.assertIs(second["pdf"]["regenerated"], False)
		# The same page, not a fresh one drawn on a retry.
		self.assertEqual(second["pdf"]["file_url"], first["pdf"]["file_url"])

	def test_47_a_retry_on_a_form_nobody_rendered_does_not_grow_a_page(self):
		"""The other half of the rule above. `include_pdf` asks for a page and
		the already-signed branch answers with what is there — which on a form
		signed in the Desk is nothing."""
		self.the_hr_furniture()
		self.an_unsigned_i9()
		self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=A_PNG,
			include_pdf=False,
		)
		second = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=A_PNG,
		)
		self.assertIs(second["already_signed"], True)
		self.assertIs(second["pdf"]["available"], False)
		self.assertFalse(frappe.db.get_value("I-9 Form", "I9-2026-CONTRACT", "generated_pdf") or "")

	def test_47_a_retry_whose_answer_was_lost_reports_success(self):
		"""§14.4. A worker shown an error for a signature that landed is a worker
		who signs again, and nothing is overwritten to say so."""
		self.the_hr_furniture()
		self.an_unsigned_i9()
		first = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=A_PNG,
		)
		second = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=A_PNG,
		)
		SignatureSubmissionModel.decode(second, "submit_form_signature")
		self.assertIs(second["already_signed"], True)
		# The ink on the record is the FIRST one. An attestation is replaced
		# deliberately or not at all.
		self.assertEqual(
			frappe.db.get_value("I-9 Form", "I9-2026-CONTRACT", "section_1_signature"),
			first["file_url"],
		)

	def test_47_the_evidence_the_pad_sends_reaches_the_register(self):
		"""v0.60.0. §14.2's `gps_lat`/`gps_lon` were DROPPED by `routes.bind`
		because the signature did not declare them, and the reason it did not was
		that the server had nowhere to put a location. It has one now, and the
		badge, the device and the fix travel with it — a signature event that says
		who was proved to be at the pad, on what, and where."""
		self.the_hr_furniture()
		self.an_unsigned_i9()
		STORE.seed(
			"Bucket Log Badge Map",
			[
				{
					"doctype": "Bucket Log Badge Map",
					"name": "ETC-0007",
					"badge_id": "ETC-0007",
					"employee": self.NEW_HIRE,
					"company": MAIN,
					"active": 1,
				}
			],
		)
		row = self.wire(
			"submit_form_signature",
			doctype="I-9 Form",
			docname="I9-2026-CONTRACT",
			signature_field="section_1_signature",
			signature_image=A_PNG,
			signer_role="employee",
			signer_badge="ETC-0007",
			device_id="9E1C4A70-0B2F-4C1E-9A55-1D7E0F3B2C48",
			gps_lat=45.5231,
			gps_lon=-122.6765,
			include_pdf=False,
		)
		SignatureSubmissionModel.decode(row, "submit_form_signature")
		self.assertEqual(row["evidence_status"], "Recorded")
		evidence = frappe.db.get_value(
			"Signing Evidence",
			row["evidence"],
			["signer", "signer_badge", "verification_method", "device_id", "gps_latitude"],
			as_dict=True,
		)
		self.assertEqual(evidence["signer"], self.NEW_HIRE)
		self.assertEqual(evidence["signer_badge"], "ETC-0007")
		self.assertEqual(evidence["verification_method"], "Badge QR")
		self.assertTrue(evidence["device_id"])
		self.assertAlmostEqual(float(evidence["gps_latitude"]), 45.5231, places=4)

	def test_47_a_badge_naming_somebody_else_stops_the_pad(self):
		"""The identity step, on the door the phone actually posts to. Either the
		wrong person is at the pad or the wrong form is open, and nothing is
		written either way."""
		self.the_hr_furniture()
		self.an_unsigned_i9()
		STORE.seed(
			"Bucket Log Badge Map",
			[
				{
					"doctype": "Bucket Log Badge Map",
					"name": "ETC-0008",
					"badge_id": "ETC-0008",
					"employee": self.SECOND_HAND,
					"company": MAIN,
					"active": 1,
				}
			],
		)
		with self.assertRaises(Exception) as caught:
			self.wire(
				"submit_form_signature",
				doctype="I-9 Form",
				docname="I9-2026-CONTRACT",
				signature_field="section_1_signature",
				signature_image=A_PNG,
				signer_badge="ETC-0008",
			)
		self.assertIn("Nothing was changed", str(caught.exception))
		self.assertFalse(frappe.db.get_value("I-9 Form", "I9-2026-CONTRACT", "section_1_signature") or "")
		self.assertEqual(STORE.rows("Signing Evidence"), [])

	def test_45_the_photo_and_the_badge_meet_on_the_card(self):
		"""The two halves of this release, in the order the wizard walks them:
		the headshot is filed, and the badge issued after it carries the URL the
		print template reads. This is the join the feature is for."""
		self.the_hr_furniture()
		_staged, finalized = self.upload(kind="profile-photo", name="employee_photo.jpg")
		photo = self.wire("set_employee_photo", employee=self.NEW_HIRE, file_token=finalized["file_token"])

		row = self.wire("generate_employee_badge_qr", employee=self.NEW_HIRE, company=MAIN)
		IssuedBadgeModel.decode(row, "generate_employee_badge_qr")
		self.assertEqual(row["photo_url"], photo["photo_url"])

	# ── v0.62.0: the seven the handset called and this surface 404'd on ─────
	#
	# EVERY ONE OF THESE IS A ROUTE THE APP ALREADY NAMES. `MobileAPI.swift` was
	# audited against v0.61.0 on 2026-08-12 and its own comments record what each
	# 404 cost: an Assignment step that read four dropdowns off the site and could
	# not write the answers, a Housing step with no server to talk to under the
	# name it used, a contact step collecting a number that went nowhere, and a
	# folder of private files that could be filled and never opened.
	#
	# THREE OF THE SEVEN REACH THE SAME FUNCTIONS AS METHODS ALREADY TESTED
	# ABOVE, under the name and the argument spellings the app posts. What these
	# tests are FOR is not the shared body — it has its own tests — but the two
	# things a bare rename would have got wrong: that the arguments the handset
	# sends are declared (`routes.bind` drops what a signature does not name), and
	# that the answer decodes into the Codable the app actually uses.

	def test_48_list_org_reference_data(self):
		"""§13.1 under the name the app calls it. `list_onboarding_reference_data`
		has served this since v0.54.0 and `OnboardingAPI.orgReferenceData` posts
		to a path that did not exist, so the four pickers came back
		`.unavailable` on every hire and the step showed a sentence about setting
		them in the office."""
		self.the_hr_furniture()
		body = self.wire("list_org_reference_data", company=MAIN)
		OrgReferenceDataModel.decode(body, "list_org_reference_data")

		self.assertIn("branches", body)
		self.assertIn("employment_types", body)
		# The same answer as the older name, key for key. Both call one function,
		# and this is what says so rather than assuming it.
		self.assertEqual(body, self.wire("list_onboarding_reference_data", company=MAIN))

	def test_48_a_branch_carries_the_ground_it_holds(self):
		"""The join the Housing step cannot be reached without: an Employee has a
		Branch, a cabin stands on a Parcel, and `Parcel.branch` is the only column
		between them."""
		self.the_hr_furniture()
		STORE.seed("Branch", [{"name": "Mill Creek Camp", "branch": "Mill Creek Camp"}])
		# The docname, not the label. A Parcel is docnamed with the owning
		# entity's abbreviation on it, and reading it off the row rather than
		# spelling it here is the same rule `OrgOption` states about Links.
		parcel = frappe.db.get_value("Housing Unit", self.unit, "parcel")
		frappe.db.set_value("Parcel", parcel, "branch", "Mill Creek Camp")

		body = self.wire("list_org_reference_data")
		OrgReferenceDataModel.decode(body, "list_org_reference_data")
		row = next(entry for entry in body["branches"] if entry["name"] == "Mill Creek Camp")
		self.assertEqual(row["parcels"], [parcel])
		# One parcel, so the scalar convenience is filled. Null is the answer for
		# none AND for several — see the mirror.
		self.assertEqual(row["parcel"], parcel)

	def test_49_list_housing_units(self):
		"""§13.3 under the app's name, and with the app's spelling of its filter.

		`HousingAPI.listUnits` sends `assignable_only`; `list_available_housing`
		declares `include_full`. A rename alone would have turned the 404 into a
		filter `routes.bind` drops on the floor — the harder bug to notice,
		because the list comes back looking fine and full of cabins nobody can be
		put in.
		"""
		self.the_hr_furniture()
		body = self.wire("list_housing_units", company=MAIN)
		HousingUnitListModel.decode(body, "list_housing_units")

		unit = next(row for row in body["units"] if row["name"] == self.unit)
		self.assertEqual(unit["capacity"], 4)
		self.assertEqual(unit["occupied"], 0)
		self.assertIs(unit["assignable"], True)
		self.assertIs(unit["is_residential"], True)
		self.assertIsNone(unit["blocked_reason"])
		self.assertEqual(body["total_open_beds"], body["open_beds"])

	def test_49_assignable_only_is_the_negative_of_include_full(self):
		"""And the DEFAULT flips with it. The handset sends the flag only to
		narrow, so the ordinary call asks for the whole camp — the full cabins
		greyed out with the reason printed, because a foreman who cannot find the
		cabin they expected needs to be told it is full rather than shown a
		shorter list."""
		self.the_hr_furniture()
		frappe.db.set_value("Housing Unit", self.unit, "condition", "Uninhabitable")

		wide = self.wire("list_housing_units", company=MAIN)
		HousingUnitListModel.decode(wide, "list_housing_units")
		condemned = next(row for row in wide["units"] if row["name"] == self.unit)
		self.assertIs(condemned["assignable"], False)
		self.assertIn("Uninhabitable", condemned["blocked_reason"])

		narrow = self.wire("list_housing_units", company=MAIN, assignable_only=True)
		HousingUnitListModel.decode(narrow, "list_housing_units")
		self.assertNotIn(self.unit, [row["name"] for row in narrow["units"]])
		# Which is the older method's DEFAULT, and the point of declaring the
		# argument at all: the same camp, two answers, one flag.
		self.assertEqual(
			[row["name"] for row in narrow["units"]],
			[row["name"] for row in self.wire("list_available_housing", company=MAIN)["units"]],
		)

	def test_50_create_housing_assignment(self):
		"""§13.4 under the name and the four argument spellings the app posts.

		`OnboardingHousing.apiParams` sends `unit` and `assigned_date`;
		`assign_housing` declares `housing_unit` and `check_in_date`. A rename
		alone would have delivered a body with no cabin and no date in it, and
		refused every hire for want of a start date it had been sent.
		"""
		self.the_hr_furniture()
		row = self.wire(
			"create_housing_assignment",
			employee=self.NEW_HIRE,
			unit=self.unit,
			company=MAIN,
			assigned_date=frappe.utils.today(),
			housing_deduction_from_wages="No",
			notes="Key #14 handed over",
		)
		HousingAssignmentResultModel.decode(row, "create_housing_assignment")

		self.assertTrue(row["name"])
		self.assertEqual(row["unit"], self.unit)
		self.assertEqual(row["employee"], self.NEW_HIRE)
		self.assertEqual(row["assigned_date"], frappe.utils.today())
		self.assertEqual(row["occupied"], 1)
		self.assertEqual(row["open_beds"], 3)
		# `assignment` is v0.54.0's key for the same docname and is still sent.
		self.assertEqual(row["assignment"], row["name"])

	def test_50_the_second_body_needs_the_barracks_flag_said_out_loud(self):
		"""A bunk room and a double-tap look identical on the wire, and the app
		resolves that where it can be resolved: the foreman is standing there.
		Off by default, so the typo is refused NAMING who is already in the
		cabin, and the flag is the deliberate second tap."""
		self.the_hr_furniture()
		first = self.wire(
			"create_housing_assignment",
			employee=self.NEW_HIRE,
			unit=self.unit,
			assigned_date=frappe.utils.today(),
		)
		HousingAssignmentResultModel.decode(first, "create_housing_assignment")

		with self.assertRaises(Exception) as caught:
			self.wire(
				"create_housing_assignment",
				employee=self.SECOND_HAND,
				unit=self.unit,
				assigned_date=frappe.utils.today(),
			)
		# The refusal NAMES who is already in there, which is what makes it
		# actionable at a tailgate rather than a mystery.
		self.assertIn("allow_multi_occupancy", str(caught.exception))
		# By NAME, not by docname — the foreman reads this at a tailgate and
		# "HA-2026-07-00001" tells them nothing about who is in the cabin.
		self.assertIn("Rosa Delgado", str(caught.exception))

	def test_50_the_flag_lets_the_second_body_in_under_capacity(self):
		"""The other half of the same rule: a bunk room legitimately holds six,
		and the flag is how a foreman says this one really is shared."""
		self.the_hr_furniture()
		self.wire(
			"create_housing_assignment",
			employee=self.NEW_HIRE,
			unit=self.unit,
			assigned_date=frappe.utils.today(),
		)
		shared = self.wire(
			"create_housing_assignment",
			employee=self.SECOND_HAND,
			unit=self.unit,
			assigned_date=frappe.utils.today(),
			allow_multi_occupancy=True,
		)
		HousingAssignmentResultModel.decode(shared, "create_housing_assignment")
		self.assertEqual(shared["occupied"], 2)
		self.assertEqual(shared["open_beds"], 2)

	def test_50_the_flag_does_not_lift_the_capacity_ceiling(self):
		"""THE ONE PROPERTY THAT MUST NOT DRIFT. Nothing on a phone adds a bunk to
		a cabin, and a bed that does not exist becomes somebody sleeping in a
		truck. The tool WARNS about an over-full unit and writes the row, which is
		right on a console where an operator can weigh it; this surface refuses,
		with or without the flag."""
		self.the_hr_furniture()
		frappe.db.set_value("Housing Unit", self.unit, "capacity", 1)
		self.wire(
			"create_housing_assignment",
			employee=self.NEW_HIRE,
			unit=self.unit,
			assigned_date=frappe.utils.today(),
		)

		with self.assertRaises(Exception) as caught:
			self.wire(
				"create_housing_assignment",
				employee=self.SECOND_HAND,
				unit=self.unit,
				assigned_date=frappe.utils.today(),
				allow_multi_occupancy=True,
			)
		self.assertIn("already has 1 assigned", str(caught.exception))
		self.assertIn("Nothing was created", str(caught.exception))

	def test_49_both_doors_answer_to_both_spellings_of_the_filter(self):
		"""v0.63.1. The mirror of the bug v0.62.0 fixed, and the reason it needed
		fixing on both sides: `routes.bind` drops what a signature does not name
		whichever direction the caller crosses in. Before this, `include_full`
		sent at `list_housing_units` vanished exactly as `assignable_only` sent at
		`list_available_housing` did — and both vanish silently, which on this
		read is a wrong list of beds rather than an error anybody sees."""
		self.the_hr_furniture()
		frappe.db.set_value("Housing Unit", self.unit, "condition", "Uninhabitable")

		def names(method, **kwargs):
			answer = self.wire(method, company=MAIN, **kwargs)
			HousingUnitListModel.decode(answer, method)
			return [row["name"] for row in answer["units"]]

		# The narrow question, asked four ways at two doors. Same camp, same answer.
		narrow = names("list_available_housing")
		self.assertNotIn(self.unit, narrow)
		self.assertEqual(names("list_available_housing", assignable_only=True), narrow)
		self.assertEqual(names("list_housing_units", assignable_only=True), narrow)
		self.assertEqual(names("list_housing_units", include_full=False), narrow)

		# And the wide one. The condemned cabin is LISTED and marked, which is the
		# whole point of the handset's default.
		wide = names("list_housing_units")
		self.assertIn(self.unit, wide)
		self.assertEqual(names("list_housing_units", assignable_only=False), wide)
		self.assertEqual(names("list_available_housing", include_full=True), wide)
		self.assertEqual(names("list_available_housing", assignable_only=False), wide)

	def test_49_neither_default_moved_when_the_other_spelling_arrived(self):
		"""The property the aliases exist to protect. A body naming NEITHER
		spelling is every handset already in an orchard, and the two doors answer
		it differently on purpose: one is asked "where can somebody sleep", the
		other "show me the camp"."""
		self.the_hr_furniture()
		frappe.db.set_value("Housing Unit", self.unit, "condition", "Uninhabitable")

		self.assertNotIn(
			self.unit, [row["name"] for row in self.wire("list_available_housing", company=MAIN)["units"]]
		)
		self.assertIn(
			self.unit, [row["name"] for row in self.wire("list_housing_units", company=MAIN)["units"]]
		)

	def test_49_a_body_that_says_both_to_one_effect_is_refused_by_name(self):
		"""Not resolved. `include_full=true` beside `assignable_only=true` asked
		for the whole camp and for the open beds in one breath, and picking a half
		would answer a question nobody asked with a list somebody acts on."""
		self.the_hr_furniture()
		for door in ("list_available_housing", "list_housing_units"):
			for contradiction in (
				{"include_full": True, "assignable_only": True},
				{"include_full": False, "assignable_only": False},
			):
				with self.subTest(method=door, body=contradiction), self.assertRaises(Exception) as caught:
					self.wire(door, company=MAIN, **contradiction)
				self.assertIn("include_full", str(caught.exception))
				self.assertIn("assignable_only", str(caught.exception))
				self.assertIn("Nothing was read", str(caught.exception))

	def test_50_both_doors_answer_to_both_spellings_of_the_cabin_and_the_date(self):
		"""v0.63.1. `assign_housing` declares `housing_unit`/`check_in_date` and
		`create_housing_assignment` declares `unit`/`assigned_date`, and until now
		crossing between them lost both fields to `bind` — a hire refused for want
		of a start date the body had carried, naming an argument the caller had
		never heard of."""
		self.the_hr_furniture()
		older = self.wire(
			"assign_housing",
			employee=self.NEW_HIRE,
			unit=self.unit,
			assigned_date=frappe.utils.today(),
		)
		HousingAssignmentResultModel.decode(older, "assign_housing")
		self.assertEqual(older["unit"], self.unit)
		self.assertEqual(older["assigned_date"], frappe.utils.today())

		newer = self.wire(
			"create_housing_assignment",
			employee=self.SECOND_HAND,
			housing_unit=self.unit,
			check_in_date=frappe.utils.today(),
			allow_multi_occupancy=True,
		)
		HousingAssignmentResultModel.decode(newer, "create_housing_assignment")
		self.assertEqual(newer["unit"], self.unit)
		self.assertEqual(newer["assigned_date"], frappe.utils.today())

	def test_50_the_refusal_quotes_the_spelling_the_body_actually_used(self):
		"""Which is the whole reason the label travels with the value. A phone
		told `check_in_date is required` by a method it called with
		`assigned_date` is a phone whose operator cannot act on the sentence."""
		self.the_hr_furniture()
		with self.assertRaises(Exception) as caught:
			self.wire("assign_housing", employee=self.NEW_HIRE, unit=self.unit)
		self.assertIn("check_in_date is required", str(caught.exception))

		with self.assertRaises(Exception) as caught:
			self.wire("assign_housing", employee=self.NEW_HIRE, housing_unit=self.unit)
		self.assertIn("check_in_date is required", str(caught.exception))

		with self.assertRaises(Exception) as caught:
			self.wire(
				"create_housing_assignment",
				employee=self.NEW_HIRE,
				housing_unit=self.unit,
				assigned_date="",
			)
		self.assertIn("assigned_date is required", str(caught.exception))

	def test_50_two_spellings_naming_two_cabins_is_refused_rather_than_resolved(self):
		"""One of them is a cabin somebody is NOT being put in, and nothing in the
		body says which. Nothing is written either way."""
		self.the_hr_furniture()
		with self.assertRaises(Exception) as caught:
			self.wire(
				"create_housing_assignment",
				employee=self.NEW_HIRE,
				unit=self.unit,
				housing_unit="MC-Cabin-99",
				assigned_date=frappe.utils.today(),
			)
		self.assertIn("unit", str(caught.exception))
		self.assertIn("housing_unit", str(caught.exception))
		self.assertIn("Nothing was written", str(caught.exception))
		self.assertEqual(frappe.db.count("Housing Assignment"), 0)

	def test_51_set_employee_org_fields(self):
		"""§13.2 — the write the Assignment step has never had.

		`list_onboarding_reference_data` has served the four dropdowns since
		v0.54.0 and this surface published nothing that could put the chosen
		values on the record. So the step read beautifully off the site, asked a
		foreman four questions, and dropped all four answers.
		"""
		self.the_hr_furniture()
		STORE.seed("Branch", [{"name": "Mill Creek Camp", "branch": "Mill Creek Camp"}])
		STORE.seed("Department", [{"name": "Orchard", "department_name": "Orchard", "company": MAIN}])
		STORE.seed("Designation", [{"name": "Picker", "designation_name": "Picker"}])
		STORE.seed("Employment Type", [{"name": "Seasonal Worker", "employee_type_name": "Seasonal Worker"}])

		row = self.wire(
			"set_employee_org_fields",
			employee=self.NEW_HIRE,
			company=MAIN,
			branch="Mill Creek Camp",
			department="Orchard",
			designation="Picker",
			employment_type="Seasonal Worker",
		)
		AppliedOrgFieldsModel.decode(row, "set_employee_org_fields")

		self.assertEqual(row["employee"], self.NEW_HIRE)
		self.assertEqual(row["branch"], "Mill Creek Camp")
		self.assertEqual(row["designation"], "Picker")
		self.assertEqual(row["skipped"], [])
		# READ BACK OFF THE RECORD, not echoed off the request — which is the
		# whole reason this method answers with anything at all.
		self.assertEqual(frappe.db.get_value("Employee", self.NEW_HIRE, "designation"), "Picker")

	def test_51_an_unsent_field_is_left_alone(self):
		"""The step is shown to returning workers whose department was set in the
		office last season, and a call that wrote "" for every untouched picker
		would clear four columns somebody filled in deliberately."""
		self.the_hr_furniture()
		STORE.seed("Designation", [{"name": "Picker", "designation_name": "Picker"}])
		STORE.seed("Department", [{"name": "Orchard", "department_name": "Orchard", "company": MAIN}])
		frappe.db.set_value("Employee", self.NEW_HIRE, "department", "Orchard")

		row = self.wire("set_employee_org_fields", employee=self.NEW_HIRE, designation="Picker")
		AppliedOrgFieldsModel.decode(row, "set_employee_org_fields")
		self.assertEqual(row["department"], "Orchard")
		self.assertEqual(frappe.db.get_value("Employee", self.NEW_HIRE, "department"), "Orchard")

	def test_52_set_employee_contact_fields(self):
		"""§16 — how to reach somebody, and who to ring if something happens.

		THE ARGUMENT NAMES ARE THE HANDSET'S AND THE COLUMNS ARE FRAPPE HR'S.
		`emergency_contact_name` is `person_to_be_contacted` on the doctype and
		`emergency_phone` is `emergency_phone_number`; a phone should not have to
		know the docname of a column to file a number in it.
		"""
		self.the_hr_furniture()
		row = self.wire(
			"set_employee_contact_fields",
			employee=self.NEW_HIRE,
			cell_phone="5415550143",
			personal_email="rosa@example.test",
			current_address="144 Orchard Lane, The Dalles OR",
			emergency_contact_name="Marisol Delgado",
			emergency_phone="5415550188",
		)

		self.assertEqual(row["cell_phone"], "5415550143")
		self.assertEqual(row["emergency_contact_name"], "Marisol Delgado")
		self.assertEqual(row["skipped"], [])
		self.assertEqual(frappe.db.get_value("Employee", self.NEW_HIRE, "cell_number"), "5415550143")
		self.assertEqual(
			frappe.db.get_value("Employee", self.NEW_HIRE, "person_to_be_contacted"),
			"Marisol Delgado",
		)

	def test_52_the_two_the_shipped_app_sends_are_enough_on_their_own(self):
		"""`OnboardingContact.apiParams` sends exactly `cell_phone` and
		`personal_email`, and only when they were answered. A method that
		required the other three would refuse the call the app actually makes."""
		self.the_hr_furniture()
		row = self.wire(
			"set_employee_contact_fields",
			employee=self.NEW_HIRE,
			cell_phone="5415550143",
			personal_email="rosa@example.test",
		)
		self.assertEqual(
			frappe.db.get_value("Employee", self.NEW_HIRE, "personal_email"), "rosa@example.test"
		)
		# An untouched box is not an answer — the returning worker's number stays.
		self.assertIsNone(row["current_address"])

	def test_53_list_attachments(self):
		"""§15.1 — the missing half of every upload on this surface.

		Six routes here file documents against an Employee and none could ask
		what was already there, so a badge issued on a hire day was never visible
		from a handset again and "is there work authorization on file" was a Desk
		question.
		"""
		self.the_hr_furniture()
		_staged, finalized = self.upload(kind="licence", name="licence.jpg")
		self.wire(
			"attach_onboarding_document",
			employee=self.NEW_HIRE,
			file_token=finalized["file_token"],
			document_kind="i9_list_b_document",
		)

		body = self.wire("list_attachments", doctype="Employee", docname=self.NEW_HIRE)
		self.assertEqual(body["count"], 1)
		row = body["attachments"][0]
		EmployeeAttachmentModel.decode(row, "list_attachments", ".attachments[0]")
		self.assertEqual(row["file_name"], "licence.jpg")
		self.assertIs(row["is_private"], True)
		self.assertIs(row["retrievable"], True)

	def test_53_a_doctype_this_surface_does_not_read_is_refused(self):
		"""A CLOSED LIST, for the reason `attach_onboarding_document` names one
		parent in code. The tool takes any doctype on the site, which is right on
		a console and would be a way to walk the File table one docname at a time
		from an orchard."""
		self.the_hr_furniture()
		with self.assertRaises(frappe.PermissionError) as caught:
			self.wire("list_attachments", doctype="Journal Entry", docname="JE-0001")
		self.assertIn("not a record this surface reads attachments from", str(caught.exception))

	def test_54_get_attachment_content(self):
		"""§15.2 — the bytes, because the URL beside them cannot be fetched.

		Every file this app writes is private by design, the handset
		authenticates to the sidecar rather than to Frappe, and a
		`/private/files/…` link answers that with a login page. Without this the
		list above is a list of things that cannot be opened.
		"""
		self.the_hr_furniture()
		_staged, finalized = self.upload(kind="licence", name="licence.jpg")
		self.wire(
			"attach_onboarding_document",
			employee=self.NEW_HIRE,
			file_token=finalized["file_token"],
			document_kind="i9_list_b_document",
		)
		listed = self.wire("list_attachments", doctype="Employee", docname=self.NEW_HIRE)

		row = self.wire("get_attachment_content", file=listed["attachments"][0]["name"])
		AttachmentContentModel.decode(row, "get_attachment_content")

		self.assertEqual(row["file_name"], "licence.jpg")
		self.assertEqual(base64.b64decode(row["content"]), b"licence-bytes")
		# The MCP tool's spelling of the same bytes, so a caller written against
		# the console's answer reads the same file.
		self.assertEqual(row["content"], row["content_base64"])

	def test_54_a_file_attached_to_nothing_is_refused(self):
		"""`finalize_staged_file` commits evidence UNATTACHED on purpose, and
		`attach_onboarding_document` is the call that gives it a home. There is
		no parent to scope an unattached file by, and a file with no home is not
		this door's to open."""
		self.the_hr_furniture()
		_staged, finalized = self.upload(kind="loose", name="loose.jpg")

		with self.assertRaises(frappe.PermissionError) as caught:
			self.wire("get_attachment_content", file=finalized["file_token"])
		self.assertIn("attached to no document", str(caught.exception))

	# ── v0.81.0: finding a shift the handset lost the docname for ───────────
	def test_55_list_shifts(self):
		"""THE READ THAT MAKES AN OPEN SHIFT REACHABLE AGAIN.

		Every other shift method takes a docname, and the docname only ever came
		from `start_shift`'s answer — held in memory by the screen that asked. A
		dismissed sheet, a tab switch, a relaunch or a flat battery lost it, and
		the shift then stayed open with nothing able to name it: no Attendance
		rows for that crew, for that day, ever, because `end_shift` is what
		writes them.
		"""
		self.the_hr_furniture()
		shift = self.a_shift(crew_employees=[self.NEW_HIRE, self.SECOND_HAND])

		row = self.wire("list_shifts", status="Active")
		ShiftRegisterModel.decode(row, "list_shifts")

		self.assertEqual([entry["name"] for entry in row["shifts"]], [shift["name"]])
		self.assertTrue(row["shifts"][0]["open"])
		self.assertTrue(row["mine"])

	def test_55_a_closed_shift_is_not_in_the_active_answer(self):
		"""`status` is COMPUTED from the absence of an end time rather than read
		off a stored column, so closing one is what takes it out of this list —
		which is exactly the transition the recovery banner watches for."""
		self.the_hr_furniture()
		shift = self.a_shift(crew_employees=[self.NEW_HIRE])
		_staged, signature = self.upload("signature", "shift_signature.png")
		self.wire(
			"end_shift",
			shift=shift["name"],
			supervisor_signature_file_token=signature["file_token"],
		)

		row = self.wire("list_shifts", status="Active")
		self.assertEqual(row["shifts"], [])

	def test_55_the_default_answers_only_the_callers_own_shifts(self):
		"""`mine` DEFAULTS TO TRUE AND IT IS NOT A CONVENIENCE.

		Closing a shift signs an FSMA §112.161(b) supervisor review in somebody's
		name. An unfiltered register handed to a handset is a list of other
		foremen's shifts with a Close button beside each one.
		"""
		self.the_hr_furniture()
		mine = self.a_shift(crew_employees=[self.NEW_HIRE])
		STORE.tables["Farm Shift"][mine["name"]]["foreman"] = "EMP-SOMEBODY-ELSE"

		row = self.wire("list_shifts", status="Active")
		self.assertEqual(row["shifts"], [])

		everyone = self.wire("list_shifts", status="Active", mine=False)
		self.assertEqual([entry["name"] for entry in everyone["shifts"]], [mine["name"]])
		self.assertFalse(everyone["mine"])

	def test_55_the_flag_survives_the_spelling_a_form_post_uses(self):
		"""A phone sends `false`; a form post sends the STRING `"false"`, and
		`bool("false")` is True. `_as_flag` is what keeps the two the same
		answer — getting this wrong would silently widen the default from "mine"
		to "everybody's"."""
		self.the_hr_furniture()
		self.a_shift(crew_employees=[self.NEW_HIRE])
		STORE.tables["Farm Shift"][next(iter(STORE.tables["Farm Shift"]))]["foreman"] = "EMP-SOMEBODY-ELSE"

		self.assertEqual(self.wire("list_shifts", mine="false")["mine"], False)
		self.assertEqual(self.wire("list_shifts", mine="true")["mine"], True)

	# ── v0.105.0. The feedback bubble drains ────────────────────────────────
	def test_56_submit_app_feedback(self):
		"""THE ANSWER THE HANDSET READS IS A DOCNAME OR NOTHING.

		`FeedbackAPI.submit` calls `client.callVoid` and then reads a name out of
		whatever came back, best-effort. `name` is the first key it looks for, so
		emitting the docname there is what turns a receipt into one that can be
		reconciled against the queue — but a reply it cannot read is explicitly
		NOT an error, because the note is filed by the time that code runs.
		"""
		row = self.wire(
			"submit_app_feedback",
			entry_uuid="B1B2C3D4-0000-4444-8888-999999999999",
			screen="my_tasks",
			comment="The task list scrolls back to the top every time I open one.",
			submitted_at="2026-08-03 05:55:00",
			app_version="1.9.0",
		)
		FeedbackReceiptModel.decode(row, "submit_app_feedback")

		self.assertTrue(row["filed"])
		self.assertTrue(row["created"])
		self.assertTrue(str(row["name"]).startswith("AFB-2026-"))

	def test_56_a_replayed_note_answers_the_same_receipt(self):
		"""THE CASE THAT HAPPENS MOST. The queue drains a backlog in one burst
		and drains it again from the start if the burst is interrupted, so the
		second send is the ordinary one — and it has to be a success carrying
		the SAME docname, or the phone files a second copy of one complaint."""
		body = {
			"entry_uuid": "C1C2C3C4-0000-4444-8888-999999999999",
			"screen": "harvest_day",
			"comment": "The bucket count resets when I lock the phone.",
			"submitted_at": "2026-08-03 06:10:00",
		}
		first = self.wire("submit_app_feedback", **body)
		second = self.wire("submit_app_feedback", **body)

		FeedbackReceiptModel.decode(second, "submit_app_feedback")
		self.assertEqual(second["name"], first["name"])
		self.assertTrue(second["filed"])
		self.assertTrue(second["duplicate"])
		self.assertFalse(second["created"])

	def test_56_the_two_timestamps_reach_the_phone_as_strings_it_can_parse(self):
		"""`submitted_at` is when Send was pressed and `received_at` is when it
		landed — weeks apart on a farm with no signal in the blocks, which is
		exactly why both are on the receipt rather than one."""
		row = self.wire(
			"submit_app_feedback",
			entry_uuid="D1D2D3D4-0000-4444-8888-999999999999",
			screen="bucket_capture",
			comment="The shutter fires before the bucket is in frame.",
			submitted_at="2026-08-03 06:20:00",
		)
		for key in ("submitted_at", "received_at"):
			with self.subTest(key=key):
				self.assertIsInstance(row[key], str)
				self.assertTrue(row[key])

	def test_57_materialize_task_for_alert(self):
		"""THE APP DECODES A `FarmTask` FROM THIS, NOT A REPORT.

		`ComplianceAPI.createTaskFromAlert` calls `client.call(MobileAPI.materializeTaskForAlert)`
		with a `FarmTask` as the return type, and falls back to `create_farm_task`
		on a 404 — which is what every farm answered until v0.106.0. The tool
		behind this returns a REPORT (`{alert, task: "FT-…", task_type, …}`), so
		the wrapper reads the task back and shapes it; a wrapper that renamed the
		report's keys instead would decode here and be a second spelling of a
		Farm Task for `completed_at` to go missing from all over again.
		"""
		frappe.local.session.user = "Administrator"
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()

		alert = next(
			row
			for row in self.wire("list_compliance_alerts")["alerts"]
			if row["name"].startswith("housing_inspection_overdue")
		)
		row = self.wire("materialize_task_for_alert", alert=alert["name"], urgency="High")
		FarmTaskModel.decode(row, "materialize_task_for_alert")

		self.assertTrue(str(row["name"]).startswith("FT-"))
		# THE EDGE THE FALLBACK CANNOT DRAW. `create_farm_task` does not declare
		# `source_alert`, so a task raised the long way round is not linked to the
		# alert it answers — nothing closes the alert and the sweep raises it
		# again the same night beside the task somebody is holding.
		self.assertEqual(row["source_alert"], alert["name"])
		self.assertEqual(row["urgency"], "High", "the caller's urgency beat the severity's")
		self.assertFalse(row["already_answered"])

	def test_57_a_second_tap_returns_the_first_tap_s_task(self):
		"""IDEMPOTENT, AND THE PHONE HAS TO BE ABLE TO SAY SO. A foreman who taps
		twice on a slow connection must not raise two inspections of one cabin,
		and a client that reported "created" both times would be lying to them."""
		frappe.local.session.user = "Administrator"
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()

		alert = next(
			row
			for row in self.wire("list_compliance_alerts")["alerts"]
			if row["name"].startswith("housing_inspection_overdue")
		)
		first = self.wire("materialize_task_for_alert", alert=alert["name"])
		second = self.wire("materialize_task_for_alert", alert=alert["name"])

		FarmTaskModel.decode(second, "materialize_task_for_alert")
		self.assertEqual(second["name"], first["name"])
		self.assertFalse(first["already_answered"])
		self.assertTrue(second["already_answered"])

	def test_57_a_field_worker_may_not_raise_one(self):
		"""`guard.require_dispatch_role`, same as `create_farm_task`. Raising work
		onto somebody else's list is a foreman's, and a route that let a picker do
		it from an alert would be the dispatch gate with a second door in it."""
		frappe.local.session.user = "Administrator"
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})
		set_roles(WORKER, ["Field Worker"])
		self.be()

		alert = next(
			row
			for row in self.wire("list_compliance_alerts")["alerts"]
			if row["name"].startswith("housing_inspection_overdue")
		)
		with self.assertRaises(frappe.PermissionError):
			self.wire("materialize_task_for_alert", alert=alert["name"])


# ── 2. the mirrors are strict enough to have caught the bugs ────────────────
class TheMirrorsAreStrictEnough(ContractTestCase):
	"""A transcription that accepts the broken payload proves nothing at all.

	Every mirror above is fed the exact payload from the release that shipped
	broken, and has to refuse it. Without these, this whole file could be a
	no-op and every test in it would still be green.
	"""

	def test_the_v0_18_2_claim_task_payload_is_refused_by_name(self):
		"""`{"name": null, …}` — what iOS reported as "Bad value at 'name'"."""
		broken = dict(self.wire("get_task", task=self.task))
		broken["name"] = None
		with self.assertRaises(ContractError) as caught:
			FarmTaskModel.decode(broken, "claim_task")
		self.assertIn("v0.18.2", str(caught.exception))
		self.assertIn("claim_task", str(caught.exception))
		self.assertIn("FarmTask.swift:105", str(caught.exception))

	def test_a_missing_strict_field_is_refused_and_names_the_swift_line(self):
		broken = dict(self.wire("get_task", task=self.task))
		broken.pop("name")
		with self.assertRaises(ContractError) as caught:
			FarmTaskModel.decode(broken, "get_task")
		self.assertIn("FarmTask.swift:105", str(caught.exception))

	def test_a_user_context_whose_company_has_no_name_is_refused(self):
		"""The nested strict field. One bad row in `companies` throws the lot."""
		broken = dict(self.wire("get_current_user_context"))
		broken["companies"] = [{"name": None, "abbr": "MC"}]
		with self.assertRaises(ContractError) as caught:
			UserContextModel.decode(broken, "get_current_user_context")
		self.assertIn("UserContext.swift:17", str(caught.exception))
		self.assertIn("companies[0]", str(caught.exception))

	def test_a_state_outside_the_swift_enum_is_refused(self):
		broken = dict(self.wire("get_task", task=self.task))
		broken["state"] = "Paused"
		with self.assertRaises(ContractError) as caught:
			FarmTaskModel.decode(broken, "get_task")
		self.assertIn("Unrecognized", str(caught.exception))

	def test_a_timestamp_frappe_date_cannot_parse_is_refused(self):
		broken = dict(self.wire("get_task", task=self.task))
		broken["claimed_at"] = "02/08/2026 9:14 PM"
		with self.assertRaises(ContractError) as caught:
			FarmTaskModel.decode(broken, "get_task")
		self.assertIn("FrappeDate", str(caught.exception))

	def test_a_number_where_a_lenient_string_belongs_is_refused(self):
		"""Silent, not fatal — `lenientString` decodes String and nothing else,
		so an Int `company` simply is not on the screen."""
		broken = dict(self.wire("get_task", task=self.task))
		broken["company"] = 7
		with self.assertRaises(ContractError) as caught:
			FarmTaskModel.decode(broken, "get_task")
		self.assertIn("silently", str(caught.exception))

	def test_a_created_employee_with_no_name_is_refused_by_the_step_that_needs_it(self):
		"""Five later calls are addressed to this string. Null here is v0.18.2 again."""
		with self.assertRaises(ContractError) as caught:
			CreatedEmployeeModel.decode({"name": None, "employee_id": "042"}, "create_employee")
		self.assertIn("v0.18.2", str(caught.exception))
		self.assertIn("OnboardingAPI.swift:16", str(caught.exception))

	def test_a_numeric_employee_id_is_refused_even_though_the_field_is_optional(self):
		"""The NULLABLE rule earning its place. `employee_number` is a Data field on
		a stock site and an Int on one where somebody made it a number — and
		`decodeIfPresent(String.self)` throws on the second, optional or not."""
		with self.assertRaises(ContractError) as caught:
			CreatedEmployeeModel.decode({"name": "HR-EMP-00042", "employee_id": 42}, "create_employee")
		self.assertIn("synthesized decoder", str(caught.exception))
		self.assertIn("OnboardingAPI.swift:17", str(caught.exception))

	def test_an_absent_employee_id_is_accepted_because_the_app_accepts_it(self):
		"""Not stricter than the app: a site that keeps no payroll numbers is fine,
		and the wizard falls back to the docname."""
		CreatedEmployeeModel.decode({"name": "HR-EMP-00042", "employee_id": None}, "create_employee")

	def test_a_search_row_with_no_employee_name_takes_the_whole_list_down(self):
		with self.assertRaises(ContractError) as caught:
			ExistingEmployeeModel.decode({"name": "EMP-1"}, "search_employees", ".employees[0]")
		self.assertIn("OnboardingModels.swift:275", str(caught.exception))
		self.assertIn("employees[0]", str(caught.exception))

	def test_a_finalize_with_neither_handle_is_refused(self):
		with self.assertRaises(ContractError) as caught:
			FinalizedFileModel.decode({"file_name": "x.jpg"}, "finalize_staged_file")
		self.assertIn("no file handle", str(caught.exception))

	def test_a_finalize_with_only_a_url_is_accepted_because_the_app_accepts_it(self):
		"""The mirror is not stricter than the app. `FinalizeResponse` falls back
		to `file_url` on purpose, and a test that refused it would send somebody
		hunting a bug that is not there."""
		FinalizedFileModel.decode(
			{"file_url": "/private/files/x.jpg", "file_name": "x.jpg"}, "finalize_staged_file"
		)


# ── 3. the mirrors cover the surface, and keep covering it ──────────────────
class TheContractIsComplete(ContractTestCase):
	"""The test that fails when somebody adds a thirteenth method and no mirror.

	Without this, the suite silently stops covering the surface the moment it
	grows — which is precisely how the app got four undecodable releases while
	a green suite watched.
	"""

	#: Method name → the test that decodes it, by number.
	COVERED: ClassVar[dict[str, str]] = {
		"get_current_user_context": "test_01",
		"list_my_tasks": "test_02",
		"list_available_tasks": "test_03",
		"get_task": "test_04",
		"claim_task": "test_05",
		"start_task": "test_06",
		"complete_task_via_mobile": "test_07",
		"reject_task": "test_08",
		"list_compliance_alerts": "test_09",
		"report_field_task": "test_10",
		"stage_file_chunk": "test_11",
		"finalize_staged_file": "test_12",
		"scan_asset": "test_13",
		"get_asset_detail": "test_14",
		"log_asset_state_change": "test_15",
		"get_available_actions": "test_16",
		"report_asset_issue": "test_17",
		"create_i9_form": "test_18",
		"submit_i9_section_1": "test_19",
		"submit_i9_section_2": "test_20",
		"submit_w4": "test_21",
		"link_badge_to_employee": "test_22",
		"sync_bucket_entries": "test_23",
		"start_shift": "test_24",
		"add_worker_to_shift": "test_25",
		"end_shift": "test_26",
		"create_employee": "test_27",
		"search_employees": "test_28",
		"reactivate_employee": "test_29",
		"get_employee": "test_30",
		"list_i9_document_types": "test_31",
		"reverify_i9": "test_32",
		# v0.47.1 — the I-9 as a document.
		"get_i9_form": "test_33",
		"generate_i9_pdf": "test_34",
		"upload_signed_i9": "test_35",
		# v0.48.0 — who may sign, and the W-4 as a document.
		"list_authorized_signers": "test_38",
		"add_authorized_signer": "test_38",
		"update_authorized_signer": "test_39",
		"remove_authorized_signer": "test_40",
		"generate_w4_pdf": "test_41",
		# v0.48.3 — the onboarding evidence that had no route and was being
		# posted to a Frappe path this app's auth hook does not cover.
		"attach_onboarding_document": "test_42",
		# v0.50.0 — the read between a scan and a name, which every other
		# badge-shaped feature was blocked on.
		"resolve_badge": "test_43",
		# v0.51.0 — the headshot the badge template reads, and the issuer the
		# handset could not reach. The tool minted CF-0001 from v0.50.0 and was
		# published on the MCP registry only, so the wizard could map a card
		# printed elsewhere and could not produce one.
		"set_employee_photo": "test_44",
		"generate_employee_badge_qr": "test_45",
		# v0.57.0 — the compliance tab stops being a noticeboard. One row may be
		# closed where the alert says it may be, and one may be signed off
		# without going via the task list first.
		"dismiss_compliance_alert": "test_46",
		"submit_form_signature": "test_47",
		# v0.62.0 — the seven `MobileAPI.swift` names and this surface answered
		# with a 404. The first three reach the same functions as
		# `list_onboarding_reference_data`, `list_available_housing` and
		# `assign_housing`; they have mirrors of their own because a shared body
		# is not a shared SIGNATURE, and `routes.bind` reduces a request to the
		# keys a signature declares.
		"list_org_reference_data": "test_48",
		"list_housing_units": "test_49",
		"create_housing_assignment": "test_50",
		"set_employee_org_fields": "test_51",
		"set_employee_contact_fields": "test_52",
		"list_attachments": "test_53",
		"get_attachment_content": "test_54",
		# v0.81.0 — the read a handset that lost the shift's docname asks. Every
		# other shift method takes a docname and there was no way to find one
		# again, so an open shift became permanently un-closeable from the app.
		"list_shifts": "test_55",
		# v0.105.0 — the in-app feedback bubble. `FeedbackAPI.swift` names it and
		# reads the reply best-effort, so the mirror has nothing strict in it —
		# see `FeedbackReceiptModel` for why that is the contract rather than a
		# gap.
		"submit_app_feedback": "test_56",
		# v0.106.0 — the compliance-alert-to-task route the app has been calling
		# and getting a 404 from since the feature shipped. `ComplianceAPI` reads
		# a `FarmTask` out of it, so the mirror is `FarmTaskModel` and not a
		# receipt shape.
		"materialize_task_for_alert": "test_57",
	}

	def _published(self, module):
		return {
			name
			for name in dir(module)
			if not name.startswith("_") and getattr(getattr(module, name), "farm_ops_method", None)
		}

	def test_every_published_method_has_a_mirror_test(self):
		# v0.52.0's model-serving pair is exempted for the same reason
		# `test_api_mobile.TheSurfaceIsClosed` excludes it from MOBILE — see
		# `PENDING_IOS_INTEGRATION` there. There is no Swift Codable to
		# transcribe yet because nothing in `fafo_ios` calls this route yet;
		# writing a "mirror" here would invent the contract rather than copy
		# one that exists.
		published = self._published(mobile_api) | self._published(files_api)
		missing = published - set(self.COVERED) - _MobileSurface.PENDING_IOS_INTEGRATION
		self.assertEqual(
			missing,
			set(),
			f"{sorted(missing)} is reachable from the app and nothing in this file decodes its "
			f"response. Add a mirror of its iOS Codable and a test_NN_ method.",
		)

	def test_no_mirror_test_names_a_method_that_no_longer_exists(self):
		published = self._published(mobile_api) | self._published(files_api)
		stale = set(self.COVERED) - published
		self.assertEqual(stale, set(), f"{sorted(stale)} is covered here and published nowhere.")

	def test_the_covering_tests_all_exist(self):
		names = {name for name in dir(EveryMobileMethodDecodes) if name.startswith("test_")}
		for method, prefix in self.COVERED.items():
			with self.subTest(method=method):
				self.assertTrue(
					any(name.startswith(prefix) for name in names),
					f"{method} claims to be covered by {prefix}_*, which is not in EveryMobileMethodDecodes.",
				)

	def test_every_swift_file_a_mirror_names_is_on_disk(self):
		"""A mirror pointing at a renamed file sends somebody to nowhere.

		Skipped rather than failed when the iOS checkout is not beside this one —
		the server suite has to run on a bench that has never seen the app.
		"""
		import os

		root = os.path.abspath(
			os.path.join(
				os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
				"..",
				"fafo_ios",
				"FarmOpsKit",
				"Sources",
				"FarmOpsKit",
			)
		)
		if not os.path.isdir(root):
			self.skipTest("the fafo_ios checkout is not beside this one")
		mirrors = (
			EvidenceContractModel,
			FarmTaskModel,
			AccessibleCompanyModel,
			UserContextModel,
			ComplianceAlertSummaryModel,
			CompletionResultModel,
			StagedChunkModel,
			FinalizedFileModel,
			VoidResponseModel,
			UndecodedResponseModel,
			FeedbackReceiptModel,
			AttachedDocumentModel,
			SignedI9Model,
		)
		for mirror in mirrors:
			with self.subTest(mirror=mirror.__name__):
				found = [
					os.path.join(where, mirror.SWIFT)
					for where in (
						os.path.join(root, "Models"),
						os.path.join(root, "Networking"),
					)
					if os.path.isfile(os.path.join(where, mirror.SWIFT))
				]
				self.assertTrue(found, f"{mirror.__name__} points at {mirror.SWIFT}, which is not there")


# ── 4. the fields that only matter because a person reads them ──────────────
class TheScreensHaveSomethingToShow(ContractTestCase):
	"""Decoding is not the whole contract. A payload can decode perfectly and
	still leave a worker looking at a card with nothing on it, which is the
	failure mode `shape.py`'s docstring calls worse than a crash."""

	def test_a_task_raised_by_an_alert_carries_the_sentence_the_card_renders(self):
		frappe.local.session.user = "Administrator"
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})
		alert = next(iter(STORE.rows("Compliance Alert")), None)
		if alert is None:
			self.skipTest("this fixture raised no compliance alert")
		task = self.a_task(source_alert=alert["name"])
		self.be()
		row = self.wire("get_task", task=task)
		FarmTaskModel.decode(row, "get_task")
		self.assertTrue(
			row.get("source_alert_explanation"),
			"the app hides its 'Why this task exists' card without this, and an inspection "
			"with no stated reason is worse than none",
		)

	def test_the_evidence_contract_survives_the_wire_as_an_object(self):
		"""`lenientJSON` takes an object OR a string of JSON; both work, and a
		list would silently become `.none` — a completion screen asking for
		nothing on a task that requires two photographs and a signature."""
		row = self.wire("get_task", task=self.task)
		contract = row["evidence_required"]
		self.assertIsInstance(contract, (dict, str), f"evidence_required arrived as {type(contract)}")
		if isinstance(contract, str):
			contract = json.loads(contract)
		EvidenceContractModel.decode(contract, "get_task", ".evidence_required")

	def test_the_completion_tells_the_worker_what_their_work_produced(self):
		""" "Housing Inspection HI-… created" closes the loop; "Done" does not."""
		claimed = mobile_api.claim_task(task=self.task)
		mobile_api.start_task(task=self.task)
		_staged, photo = self.upload("photo", "FT_photo.jpg")
		_staged, signature = self.upload("signature", "FT_signature.png")
		row = self.wire(
			"complete_task_via_mobile",
			task=self.task,
			task_assignment=claimed["assignment"],
			findings_text="",
			clean_pass=True,
			evidence_files=[
				{"file_token": photo["file_token"], "kind": "photo"},
				{"file_token": signature["file_token"], "kind": "signature"},
			],
		)
		self.assertTrue(row["created_record_name"], "the worker is told nothing was produced")
		self.assertTrue(row["created_record_doctype"])
