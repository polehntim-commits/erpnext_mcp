# SPDX-License-Identifier: MIT
"""Collecting the signature a compliance alert said was missing. v0.55.0.

THE OTHER END OF `i9_section_1_unsigned` AND ITS FOUR SIBLINGS. Those rules
find the boxes nobody signed; `generate_tasks_from_compliance_alerts` turns each
into a Farm Task held by the person who can fix it; and this is what that person
calls when they have got the signature. Without it the loop stops one step short
— the app would be very good at telling an operation which forms are unsigned
and would offer no way to sign one.

────────────────────────────────────────────────────────────────────────────
ONE CALL DOES FOUR THINGS, AND THE ORDER IS THE WHOLE OF THE DESIGN
────────────────────────────────────────────────────────────────────────────

    store the image  →  write it onto the form  →  close the task  →  redraw the PDF

EACH STEP IS ALLOWED TO FAIL WITHOUT UNDOING THE ONE BEFORE IT, and the result
says which of the four happened. That asymmetry is deliberate and it runs the
right way round: the signature IS the compliance artefact, and the task and the
PDF are bookkeeping about it. A phone in an orchard that captured a signature,
wrote it to the federal record, and then could not reach the PDF renderer has
NOT failed — it has done the part §274a asks for. Rolling that back to keep four
things tidy would throw away the only irreplaceable one: the person who signed
has gone back to work and is not signing again.

The reverse ordering is what would be wrong. Closing the task first and then
failing to store the image would leave a board saying the signature was
collected and a form saying it was not.

────────────────────────────────────────────────────────────────────────────
WHY THIS ONE TAKES BASE64 WHEN `attach_signed_i9` REFUSES TO
────────────────────────────────────────────────────────────────────────────

`attach_signed_i9` says, at length, that no bytes cross its boundary: a signed
I-9 is a SCAN OF A PAGE, arriving as a photograph or a multi-megabyte PDF, and
it goes up through `stage_file_chunk` / `finalize_staged_file` — 512 KB at a
time, hashed at capture, resumable over a link that drops. A second upload path
with its own size limit and its own way of failing halfway up a hill is exactly
what that refusal is protecting.

A SIGNATURE CAPTURE IS NOT THAT FILE. It is what a finger drew on a glass
rectangle: a few kilobytes of monochrome PNG, produced in one gesture, complete
or not at all. Chunking it would be three round trips and a staging session to
move less data than the JSON body around it, and every one of those round trips
is another place for a signature to be lost between the drawing and the storing
— with the person who drew it already walking away.

So both doors are open here and the ceiling says which is which:
`SIGNATURE_MAX_BYTES` is 512 KB, a hundredfold more than a real capture and far
short of a scanned page. Anything larger is a scan, and a scan belongs on
`attach_signed_i9` with the chunked upload — which the refusal says by name.
`file_token` is accepted too, for the caller that already has one.

────────────────────────────────────────────────────────────────────────────
THE FIELD IS NAMED BY THE CALLER AND CHECKED AGAINST A TABLE
────────────────────────────────────────────────────────────────────────────

`SIGNATURE_BOXES` is a closed registry of (doctype, field) pairs, and it is
closed for the reason `alerts/rules.SCANNERS` is: "write this image into that
column" is one sentence away from "write anything into any column". A caller
naming a field outside it is refused with the list — an endpoint taking an
arbitrary fieldname on an arbitrary doctype is an arbitrary write with an
Attach-shaped hat on.

Each entry also carries what has to be TRUE beside the image: the timestamp
column, the IP column, and — for the two boxes the authorized-signer roster
governs — that the caller is on it. A signature image on its own is not an
electronic signature; 8 CFR 274a.2(h) asks for the record of who made it and
when, and this is where that record gets written.

────────────────────────────────────────────────────────────────────────────
v0.56.0: THE EMPLOYER'S OWN RETURNS, AND THE TWO FORMS THAT ARE NOT SIGNED
────────────────────────────────────────────────────────────────────────────

A fifth box covers Tax Form, which is a 941, an OR-WR, an OQ or a WA-ESD —
four returns, one signature column, and which return a record is lives in
`form_type` rather than in a fifth, sixth and seventh entry here.

W-2 AND 1099-NEC ARE NOT SIGNED AND ARE NOT BOXES. Neither form has a
signature line: the recipient copies are statements, and the
penalties-of-perjury declaration for a whole batch is made once, on the W-3 or
the 1096 transmittal that accompanies it. A caller naming one gets that
sentence back rather than a list of field names to hunt through — see
`UNSIGNED_REASONS`, which exists so the refusal ends the question instead of
starting a search for a column that is not on the page.

────────────────────────────────────────────────────────────────────────────
v0.57.0: THE BOX ALSO SAYS WHAT THE SIGNER IS BEING ASKED TO SWEAR TO
────────────────────────────────────────────────────────────────────────────

Every entry now carries the wording as well as the address — the form's name,
the section's name, and the attestation itself in the government's own words —
and `request_for_alert` turns one Compliance Alert into the object
`API_CONTRACT.md` §14.1 describes, which is what lets a phone open a signature
pad straight off the compliance calendar.

THE WORDING BELONGS HERE FOR THE SAME REASON THE FIELD NAMES DO. A handset
must not paraphrase 8 CFR § 274a.2's attestation, and it must not compose one
for a form it has never heard of; the app's own contract says in as many words
that the person signing and the auditor reading the file should be looking at
the same sentence. So the sentence lives beside the column it is printed above,
in the one table that is already a reviewed claim about what each box is.

AND IT IS THE SAME TABLE THE WRITE PATH GATES ON, which is the property worth
protecting. An alert can only offer a signature route to a box
`collect_form_signature` would actually accept ink into — a pad addressed at a
column this app refuses to write is a pad that collects a signature twice.

────────────────────────────────────────────────────────────────────────────
v0.60.0: THE SIGNATURE WAS NEVER THE HARD HALF. PROVING WHO MADE IT IS.
────────────────────────────────────────────────────────────────────────────

Everything above answers "what was signed, and when". It has never answered the
question an auditor asks second — HOW DO YOU KNOW IT WAS HIM — and the honest
reply until this release was that the app knew because a phone said so.

So one call now does six things, and the sixth is `Signing Evidence`: one row
per signature event carrying the badge that was scanned, how identity was
established, the device, the coordinates, the address, and a hash of the record
AS IT WAS PRESENTED. `tools/signing_evidence.py` argues the shape of that
register; three things belong on this side of the wire:

**THE HASH IS TAKEN BEFORE THE WRITE.** "What did they see" is a fact about the
document at presentation. A fingerprint computed afterwards includes the
signature and proves only that this app had just written one.

**THE CAPACITY COMES OFF THE BOX, NOT OFF THE CALLER.** Section 1 and Section 2
of one form are a worker's attestation and an employer's, and a client that
could label them would be choosing which legal act it had just performed. A
caller may state a role and it is CHECKED against the box; it is never believed
over it.

**IDENTITY VERIFICATION IS REFUSED, NOT RECORDED, WHEN IT FAILS.** A badge that
resolves to somebody other than the person the form is about stops the call
before the image is stored. Verification that fails open is not verification —
and a `verification_method` column that could be set without a check having
happened would be worse than no column, because it would look like proof.

The row itself is best-effort and reported, for the reason the task and the PDF
are: the signature is the irreplaceable artefact and the person who drew it has
gone back to work. What is NOT best-effort is any of the checking above.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

import frappe

from .. import compat, roles
from ..args import as_bool, as_float, as_gps, as_str
from ..errors import ToolError
from ..result import ToolResult
from . import badges, files, i9, signers, signing_evidence, taxforms, w4

ALERT = "Compliance Alert"
I9_FORM = "I-9 Form"
W4_FORM = "W-4 Form"
TAX_FORM = "Tax Form"
TRAINING_SESSION = "Training Session"
FARM_TASK = "Farm Task"

#: The most a signature capture may be, in bytes. See the module docstring: it
#: is a hundredfold over a real capture and an order of magnitude under a
#: scanned page, so it separates the two transports rather than merely limiting
#: one of them.
SIGNATURE_MAX_BYTES = 512 * 1024

#: What a capture is allowed to arrive as. PNG is what every signature pad in
#: this app's iOS build produces; JPEG is what a re-encode produces. `.svg` is
#: absent for the reason `api/files.py` gives — it executes script when served —
#: and a signature is the last file on this site that should.
SIGNATURE_EXTENSIONS = (".png", ".jpg", ".jpeg")

#: Suffix on a stored capture's filename, so a private files directory can be
#: read by eye. The docname and the field are in it because one form can carry
#: four of these and "which box is this" should not need a database.
FILE_STEM = "signature"


class AlreadySignedError(ToolError):
	"""The box this call was addressed at already carries somebody's attestation.

	A `ToolError` SUBCLASS RATHER THAN A NEW KIND OF FAILURE, so every caller
	that already catches `ToolError` — the MCP dispatcher, the old
	`collect_signature` wrapper — behaves exactly as it did and reads the same
	sentence. What the type adds is one caller's ability to tell this refusal
	apart from the other eleven without matching on prose.

	THAT CALLER IS `submit_form_signature`, and the reason is a phone.
	`API_CONTRACT.md` §14.4 asks for this route to be idempotent or to say a
	specific sentence, because a submission whose answer never made it back over
	a bad link is retried — and a worker who has already signed being shown an
	error is a worker who signs again. The wrapper answers `already_signed: true`
	on this exception and success on nothing else; a class is what makes that a
	decision rather than a `"already carries" in str(exc)`.
	"""


@dataclass(frozen=True)
class SignatureBox:
	"""One box that can be signed, and everything true about signing it.

	`form_type` IS THE ROSTER GATE AND IT IS EMPTY ON THREE OF THE FIVE. Section 1
	and the W-4 are signed by the worker themselves, who is on nobody's roster and
	should not need to be. Section 2 and Supplement B are the employer attesting,
	and `tools/signers.py` holds who may do that. The employer's tax returns are
	signed by an OFFICER, and this app has no register of officers — so that box
	is gated by `write` permission on the record and nothing else, which is said
	out loud on the entry itself rather than left to be inferred from a blank.
	"""

	doctype: str
	field: str
	label: str
	#: Where the moment is written. Required — see the module docstring on
	#: 274a.2(h): the image without the timestamp is not the record.
	signed_at_field: str
	#: Where the address it arrived from is written, if the doctype has one.
	signed_ip_field: str = ""
	#: Where the coordinates it arrived from are written, if the doctype has a
	#: column for them. v0.136.0. THE SAME FIX THE EVIDENCE ROW GETS, written a
	#: second time on the form itself — not a duplicate of the register but the
	#: 8 CFR 274a.2(h) context that has to survive on the RECORD, beside the
	#: timestamp and the address, for a reader who has the form and not the
	#: register. `Signing Evidence` can fail to write and by design does not take
	#: the signature down with it; a location that lived only there would go with
	#: it. Empty on a box whose doctype has no such column, exactly like
	#: `signed_ip_field`.
	signed_gps_field: str = ""
	#: "I-9" or "W-4" where the roster gates this box; empty where it does not.
	form_type: str = ""
	#: The child table this box lives in, empty for a box on the form itself.
	child_table: str = ""
	#: What one of those rows IS, in the words a refusal should use — "reverification
	#: entry", "attendee". Written here rather than in `_child_row` because that
	#: function used to be about Supplement B and is now about any table of rows
	#: that get signed one at a time, and a message that still said
	#: "reverification" to somebody signing a training sheet would be this app
	#: describing the wrong document.
	child_label: str = "row"
	#: The tool that puts a row on the table, named in the refusal when there are
	#: none. `reverify_i9` records a reverification; `add_session_attendee` puts
	#: somebody on a sign-in sheet.
	child_hint: str = ""
	#: The column on a row naming WHO it is about, where the rows are per-person.
	#: When set, that person — not `doc.employee` — is the subject an identity
	#: check is made against: a training session has no single employee, and
	#: twelve people sign one document in their own names.
	child_subject_field: str = ""
	#: What "the next unsigned one" is ordered by. `reverification_date` picks the
	#: newest, which is what the alert was about; `idx` picks the first one down
	#: the sheet, which is the order a room is signed in.
	child_order_field: str = ""
	#: Newest-first, or first-down-the-page. See `child_order_field`.
	child_order_desc: bool = True
	#: Columns on the row that say WHO signed, filled from the roster where the
	#: caller is on one and left alone where they are not.
	name_field: str = ""
	title_field: str = ""
	#: Which rule's alert this box answers, so a collected signature can find the
	#: task that asked for it.
	alert_types: tuple = dataclass_field(default_factory=tuple)

	# ── v0.57.0: what the person at the pad is shown before they draw ───────
	#: Who is attesting, in `API_CONTRACT.md` §14.1's vocabulary. The app has
	#: captions for `employee`, `employer`, `preparer` and `witness` and keeps
	#: anything else VERBATIM, shown humanised — so a role outside those four is
	#: a wording choice here rather than a client release.
	signer_role: str = ""
	#: The caption to print over the ruled line, where this app's four captions
	#: would word it wrongly. Wins over anything the phone would compose.
	signer_label: str = ""
	#: The form as a person names it. Falls back to the doctype on the phone.
	form_label: str = ""
	#: A column on the record naming which form this is — `Tax Form.form_type` is
	#: a 941, an OR-WR, an OQ or a WA-ESD, and the signer is entitled to be told
	#: which of the four they are signing. Composed into `form_label` when set.
	form_label_field: str = ""
	#: Which part of the form. Falls back to the humanised fieldname.
	section_label: str = ""
	#: THE SENTENCE BEING SWORN TO, IN THE GOVERNMENT'S OWN WORDS. Never
	#: paraphrased on the phone and never composed there: absent, the app omits
	#: the block entirely rather than inventing what a form attests.
	attestation: str = ""

	@property
	def key(self) -> str:
		return f"{self.doctype}.{self.field}"


#: THE CLOSED REGISTRY. Five boxes, one per missing-signature rule, and adding a
#: sixth is a code change on purpose: every entry is a claim about which column
#: on which doctype is a signature and who is allowed to put one there.
SIGNATURE_BOXES = (
	SignatureBox(
		doctype=I9_FORM,
		field="section_1_signature",
		label="I-9 Section 1 (employee attestation)",
		signed_at_field="section_1_signed_at",
		signed_ip_field="section_1_signed_ip",
		signed_gps_field="section_1_signed_gps",
		alert_types=("i9_section_1_unsigned",),
		signer_role="employee",
		form_label="Form I-9, Employment Eligibility Verification",
		section_label="Section 1. Employee Information and Attestation",
		attestation=(
			"I am aware that federal law provides for imprisonment and/or fines for false "
			"statements, or the use of false documents, in connection with the completion of this "
			"form. I attest, under penalty of perjury, that this information, including my "
			"selection of the box attesting to my citizenship or immigration status, is true and "
			"correct."
		),
	),
	SignatureBox(
		doctype=I9_FORM,
		field="section_2_signature",
		label="I-9 Section 2 (employer verification)",
		signed_at_field="section_2_signed_at",
		signed_ip_field="section_2_signed_ip",
		signed_gps_field="section_2_signed_gps",
		form_type="I-9",
		name_field="verifier_name",
		title_field="verifier_title",
		alert_types=("i9_section_2_unsigned",),
		signer_role="employer",
		form_label="Form I-9, Employment Eligibility Verification",
		section_label="Section 2. Employer Review and Verification",
		attestation=(
			"I attest, under penalty of perjury, that (1) I have examined the document(s) presented "
			"by the above-named employee, (2) the above-listed document(s) appear to be genuine and "
			"to relate to the employee named, and (3) to the best of my knowledge, the employee is "
			"authorized to work in the United States."
		),
	),
	SignatureBox(
		doctype=I9_FORM,
		field="section_3_signature",
		label="I-9 Supplement B (reverification attestation)",
		signed_at_field="signed_at",
		signed_ip_field="signed_ip",
		form_type="I-9",
		child_table="reverifications",
		child_label="reverification entry",
		child_hint="reverify_i9 records one.",
		child_order_field="reverification_date",
		name_field="verifier_name",
		title_field="verifier_title",
		alert_types=("i9_supplement_b_unsigned",),
		signer_role="employer",
		form_label="Form I-9, Employment Eligibility Verification",
		section_label="Supplement B. Reverification and Rehire",
		attestation=(
			"I attest, under penalty of perjury, that to the best of my knowledge, this employee is "
			"authorized to work in the United States, and if the employee presented document(s), "
			"the document(s) I have examined appear to be genuine and to relate to the individual."
		),
	),
	SignatureBox(
		doctype=W4_FORM,
		field="signature",
		label="W-4 (employee signature)",
		signed_at_field="signed_at",
		signed_ip_field="signed_ip",
		alert_types=("w4_signature_missing",),
		signer_role="employee",
		form_label="Form W-4, Employee's Withholding Certificate",
		section_label="Step 5. Sign Here",
		attestation=(
			"Under penalties of perjury, I declare that this certificate, to the best of my "
			"knowledge and belief, is true, correct, and complete."
		),
	),
	# v0.56.0. THE EMPLOYER'S OWN RETURNS, and one box covers four form types
	# rather than four boxes covering one each: a 941, an OR-WR, an OQ and a
	# WA-ESD are one column on one doctype, and which of them a given record is
	# lives in `form_type`. The COMPLIANCE RULE is where that distinction
	# belongs — `tax_form_signature_missing` scopes to the four that have a
	# signature line and leaves W-2 and 1099-NEC alone, because neither of those
	# forms has one.
	#
	# `form_type` IS EMPTY HERE, so no roster gate, and it is the one place this
	# table says something the I-9 boxes do not. `tools/signers.py` records who
	# may sign an I-9 and who may sign a W-4; it says nothing about who may sign
	# a tax return, because that is an officer of the employer and this app has
	# no register of officers. Gating on `can_sign_i9` would refuse the
	# bookkeeper who actually signs the 941 and admit somebody whose
	# authorisation covers a different form entirely — a wrong answer either
	# way. The gate that does apply is `write` permission on the Tax Form.
	SignatureBox(
		doctype=TAX_FORM,
		field="signature",
		label="Employer tax return (penalties-of-perjury declaration)",
		signed_at_field="signed_at",
		signed_ip_field="signed_ip",
		name_field="signer_name",
		title_field="signer_title",
		alert_types=("tax_form_signature_missing",),
		# `officer` IS NOT ONE OF THE FOUR ROLES THE APP HAS A CAPTION FOR, and
		# naming it anyway is the point rather than an oversight. A 941 is signed
		# by an officer of the employer, not by "the employer or authorized
		# representative" the I-9's caption names — and §14.1a says an
		# unrecognised role is kept verbatim, shown humanised, and posted back
		# unchanged. `signer_label` is what the line actually reads, and it wins
		# over anything a phone would compose for a word it does not know.
		signer_role="officer",
		signer_label="Signature of officer or authorized agent",
		form_label="Employer tax return",
		form_label_field="form_type",
		section_label="Penalties of perjury declaration",
		attestation=(
			"Under penalties of perjury, I declare that I have examined this return, including "
			"accompanying schedules and statements, and to the best of my knowledge and belief, it "
			"is true, correct, and complete."
		),
	),
	# THE SIXTH BOX, AND THE FIRST WHERE ONE DOCUMENT IS SIGNED BY A ROOM. The
	# five above are one form, one signer, one attestation; a training sign-in
	# sheet is twelve people signing the same afternoon in their own names, which
	# is why `child_subject_field` exists and why `_identity` reads its subject
	# off the ROW rather than off `doc.employee` — a Training Session has no
	# single employee, and checking a picker's badge against a column that is not
	# there would have made every scan pass.
	#
	# NOT ROSTER-GATED, and it is the Section 1 argument rather than a new one: a
	# worker signing to say they were taught something is signing for themselves,
	# under nobody's authority but their own, and requiring the phone that
	# collects it to belong to an authorized signer would mean only the people
	# who may attest FOR the employer could collect a worker's own mark.
	#
	# NO `alert_types`, and that is honest rather than pending. There is no rule
	# that fires on an unsigned attendee row — an incomplete sheet is caught by
	# `complete_training_session` refusing to file it, which happens the same
	# afternoon and in front of the person who can fix it, rather than by an
	# alert somebody reads on Thursday.
	SignatureBox(
		doctype=TRAINING_SESSION,
		field="signature",
		label="Training session attendance (worker's own attestation)",
		signed_at_field="signed_at",
		child_table="attendees",
		child_label="attendee",
		child_hint="add_session_attendee puts somebody on the sheet, with the badge scanned at the door.",
		child_subject_field="employee",
		child_order_field="idx",
		child_order_desc=False,
		signer_role="employee",
		signer_label="Signature of person trained",
		form_label="Training session attendance record",
		form_label_field="training_type",
		section_label="Attendance and acknowledgment",
		attestation=(
			"I attest that I attended and understood the training described on this record, that "
			"the topics listed were covered, and that I had the opportunity to ask questions."
		),
	),
)

BOXES_BY_KEY = {box.key: box for box in SIGNATURE_BOXES}

#: Columns dropped from the document fingerprint on TOP OF the signature columns
#: the boxes above already name. Both are things this app writes as a
#: CONSEQUENCE of somebody signing, and a hash that moved when they did would be
#: an integrity check that fired on its own side effects.
#:
#:   * `generated_pdf` is redrawn by `_redraw` in the same call as the signature;
#:   * `status` on a Form I-9 advances from "Section 1 Complete" to "Complete"
#:     the moment Section 2 is verified.
#:
#: WHAT IS DELIBERATELY *NOT* HERE is everything substantive: the legal name, the
#: date of birth, the citizenship attestation, the document titles, numbers and
#: expiry dates, the addresses, the withholding figures. Alter any of those after
#: an attestation was made and `verify_fingerprint` says so — which is the whole
#: point of keeping the noise out. An integrity check that reports a mismatch on
#: every correctly-handled form is one nobody reads.
#:
#: THE OTHER HALF OF THE NOISE IS HANDLED IN `document_fingerprint`, which hashes
#: only the columns that HELD SOMETHING when the record was presented — so the
#: employer filling in Section 2 in August does not make the worker's July
#: attestation read as tampered. That rule belongs beside the hash rather than in
#: this table because it is about emptiness rather than about which columns are
#: signatures.
HASH_ALSO_EXCLUDES = ("generated_pdf", "status")


def hash_exclusions(doctype: str) -> tuple:
	"""Every column the document fingerprint is taken without, for one doctype.

	COMPUTED FROM `SIGNATURE_BOXES` RATHER THAN LISTED, so a sixth box cannot be
	added without its three columns leaving the hash — and a signature that
	silently invalidated every earlier fingerprint on the same form is exactly the
	failure a second hand-maintained list would produce.

	EVERY box on the doctype contributes, not just the one being signed. A Form
	I-9 is signed three times over several months; scoping the exclusion to the
	box in hand would mean the employer's Section 2 signature made the worker's
	own Section 1 fingerprint stop matching, which is a false alarm about the one
	record that must not raise them.
	"""
	columns = set(HASH_ALSO_EXCLUDES)
	for box in SIGNATURE_BOXES:
		if doctype and box.doctype != doctype:
			continue
		# `signed_gps_field` IS ON THIS LIST FOR THE REASON THE OTHER THREE ARE,
		# and leaving it off would have been the exact false alarm the docstring
		# above describes. It is written in the same breath as the image and the
		# timestamp, so an employer signing Section 2 in the packing shed would
		# otherwise change a column covered by the fingerprint the worker's
		# Section 1 evidence row recorded that morning — and the sealed page
		# would report the worker's own attestation as no longer matching the
		# record they attested to.
		for name in (box.field, box.signed_at_field, box.signed_ip_field, box.signed_gps_field):
			if name:
				columns.add(name)
	return tuple(sorted(columns))


#: alert_type → the box that alert is about. THE SAME `alert_types` THE TASK
#: LOOKUP USES, read the other way round: `_task_for` asks "which task belongs to
#: this box", and the compliance calendar asks "which box is this alert about".
#: One mapping rather than two means an alert can never offer a signature route
#: to a column this module would refuse to write.
BOXES_BY_ALERT_TYPE = {alert_type: box for box in SIGNATURE_BOXES for alert_type in box.alert_types}

#: Spellings a caller will reasonably send for each doctype. A phone composing
#: this call from a Farm Task's `subject_doctype` sends the exact docname; a
#: model composing it from a sentence sends "I9" as often as not, and each of
#: these has one unambiguous reading.
DOCTYPE_ALIASES = {
	"i-9 form": I9_FORM,
	"i9 form": I9_FORM,
	"i-9": I9_FORM,
	"i9": I9_FORM,
	"form i-9": I9_FORM,
	"w-4 form": W4_FORM,
	"w4 form": W4_FORM,
	"w-4": W4_FORM,
	"w4": W4_FORM,
	"form w-4": W4_FORM,
	# v0.56.0. A Tax Form is a 941, an OR-WR, an OQ or a WA-ESD, and a caller
	# holding one in their hand will say which — so each form TYPE resolves to
	# the doctype that holds it. `w-2` and `1099-nec` are deliberately absent:
	# neither form has a signature box, `tax_form_signature_missing` does not
	# raise on them, and resolving them here would take a caller all the way to
	# "not a signature box this app will write to" instead of the shorter, truer
	# answer that the form they are holding has no signature line on it.
	"tax form": TAX_FORM,
	"941": TAX_FORM,
	"form 941": TAX_FORM,
	"or-wr": TAX_FORM,
	"orwr": TAX_FORM,
	"oq": TAX_FORM,
	"wa-esd": TAX_FORM,
	"waesd": TAX_FORM,
	"training session": TRAINING_SESSION,
	"training": TRAINING_SESSION,
	"sign-in sheet": TRAINING_SESSION,
	"sign in sheet": TRAINING_SESSION,
	"attendance": TRAINING_SESSION,
}

#: Forms a caller may reasonably NAME and this app will never sign, with the
#: sentence that says why. Answering "that form has no signature line" is a
#: different and more useful refusal than "that is not a box I write to" — the
#: first one settles the question, and the second sends somebody looking for a
#: field name that does not exist.
UNSIGNED_FORMS = {
	"w-2": "W-2",
	"w2": "W-2",
	"form w-2": "W-2",
	"1099-nec": "1099-NEC",
	"1099nec": "1099-NEC",
	"1099": "1099-NEC",
}

UNSIGNED_REASONS = {
	"W-2": (
		"Form W-2 has no signature line anywhere on it. The employee copies are statements "
		"rather than declarations, and the penalties-of-perjury declaration for a whole batch "
		"is made once, on the Form W-3 transmittal that goes to the SSA with them."
	),
	"1099-NEC": (
		"Form 1099-NEC has no signature line, for the same reason Form W-2 has none: the "
		"declaration for a batch is made once, on the Form 1096 transmittal."
	),
}


#: doctype → how to turn a caller's argument into a docname, and how to redraw
#: the page afterwards. v0.56.0 replaced two `if doctype == I9_FORM` branches
#: with this, and the reason is that there were about to be three of them: the
#: shape "resolve, then render" is the same for every form and only the two
#: functions differ, so a table states it once and adding a fourth form is a row.
#:
#: EVERY ENTRY NAMES ITS MODULE'S OWN RESOLVER rather than reimplementing one.
#: `i9._resolve_form`, `w4._resolve_w4` and `taxforms._resolve_form` each already
#: take the aliases their own tools take and each already says the right thing
#: when nothing resolves; a fourth reading of "which W-4" written here would be a
#: fourth place for it to mean something slightly different.
FORM_HANDLERS = {
	I9_FORM: {
		"resolve": lambda args: i9._resolve_form(args),
		"render": lambda name: i9.render_i9_pdf({"i9_form": name, "overwrite": True}),
		"pdf_field": "generated_pdf",
		"renderer": "render_i9_pdf",
	},
	W4_FORM: {
		"resolve": lambda args: w4._resolve_w4(args),
		"render": lambda name: w4.render_w4_pdf({"w4_form": name, "overwrite": True}),
		"pdf_field": "generated_pdf",
		"renderer": "render_w4_pdf",
	},
	TAX_FORM: {
		"resolve": lambda args: taxforms._resolve_form(args),
		"render": lambda name: taxforms.render_tax_form_pdf({"name": name, "overwrite": True}),
		"pdf_field": "generated_pdf",
		"renderer": "render_tax_form_pdf",
	},
	# The sign-in sheet. IMPORTED INSIDE THE LAMBDA rather than at the top of
	# this module, and it is the one entry that has to be: `tools/training_sessions`
	# imports this module for `collect_form_signature`, so naming it up there
	# would close the circle at load time. The other three are imported by
	# modules that do not import back.
	TRAINING_SESSION: {
		"resolve": lambda args: _training_session_name(args),
		"render": lambda name: _render_training_sheet(name),
		"pdf_field": "generated_pdf",
		"renderer": "render_training_sign_in_sheet",
	},
}


def _training_session_name(args: dict) -> str:
	from . import training_sessions as training_session_tools

	return str(training_session_tools._resolve_session(args).get("name") or "")


def _render_training_sheet(name: str):
	from . import training_sessions as training_session_tools

	return training_session_tools.render_training_sign_in_sheet({"session": name, "overwrite": True})


# ── resolving what is being signed ──────────────────────────────────────────
def _box(args: dict) -> SignatureBox:
	"""Which signature box this call is about, or the refusal naming every one.

	TWO REFUSALS RATHER THAN ONE, and the second is the useful one. A caller
	naming a form that HAS no signature line — a W-2, a 1099-NEC — is not making
	a typo that a list of field names will fix; they are asking this app to sign
	something that is not signed. `UNSIGNED_REASONS` answers that question
	instead, because "Form W-2 has no signature line" ends the conversation and
	"not a signature box this app will write to" starts a search for a field name
	that does not exist.
	"""
	doctype = str(args.get("doctype") or args.get("form_doctype") or "").strip()
	unsigned = UNSIGNED_FORMS.get(doctype.casefold())
	if unsigned:
		raise ToolError(
			f"{UNSIGNED_REASONS[unsigned]} There is nothing on a {unsigned} for this call to "
			f"attach a signature to, and tax_form_signature_missing does not raise on one. "
			f"The employer returns that ARE signed are 941, OR-WR, OQ and WA-ESD. "
			f"Nothing was changed."
		)
	resolved = DOCTYPE_ALIASES.get(doctype.casefold(), doctype)
	fieldname = as_str(args, "field") or as_str(args, "signature_field")

	if not resolved:
		raise ToolError(
			"doctype is required — the form the signature goes on: 'I-9 Form', 'W-4 Form' or "
			"'Tax Form'. A Farm Task raised from a missing-signature alert carries it in "
			"subject_doctype. Nothing was changed."
		)
	if not fieldname:
		# The commonest case has ONE answer, so answering it beats refusing: a
		# W-4 has exactly one signature box and asking which one is a question
		# with a single possible reply.
		candidates = [box for box in SIGNATURE_BOXES if box.doctype == resolved]
		if len(candidates) == 1:
			return candidates[0]
		raise ToolError(
			f"field is required — {resolved} has more than one signature box, and which one is "
			f"being signed is the difference between an employee attesting and an employer "
			f"attesting: {', '.join(box.field for box in candidates) or '(none)'}. "
			"Nothing was changed."
		)

	box = BOXES_BY_KEY.get(f"{resolved}.{fieldname}")
	if box is None:
		raise ToolError(
			f"{resolved}.{fieldname} is not a signature box this app will write to. It takes "
			f"exactly these, and the list is closed on purpose — an endpoint that wrote an "
			f"image into any column somebody named would be an arbitrary write:\n"
			+ "\n".join(f"  - {entry.key} — {entry.label}" for entry in SIGNATURE_BOXES)
			+ "\n\nNothing was changed."
		)
	return box


def _form_name(box: SignatureBox, args: dict) -> str:
	"""The docname being signed, accepting the same aliases its own tools do."""
	given = (
		as_str(args, "form") or as_str(args, "form_name") or as_str(args, "name") or as_str(args, "docname")
	)
	inner = dict(args)
	if given:
		inner["name"] = given
	# DELEGATED, NOT REIMPLEMENTED — see `FORM_HANDLERS`.
	return str(FORM_HANDLERS[box.doctype]["resolve"](inner))


def _child_row(box: SignatureBox, doc, args: dict) -> object:
	"""Which row of a signable table is being signed, or the refusal.

	WRITTEN FOR SUPPLEMENT B AND GENERALISED WHEN THE SIXTH BOX ARRIVED. A
	training sign-in sheet is the same shape — a table whose rows are signed one
	at a time, over an afternoon rather than over years — and the only things
	that differ are what a row is CALLED, what identifies one, and which unsigned
	one is "next". All three are on the box, so this function no longer knows
	what document it is looking at.

	THREE WAYS TO NAME A ROW, and the third is what a room full of people needs.
	A docname and a 1-based position are what Supplement B took; `employee` is
	what a training session takes, because the person at the pad is identified by
	who they are and not by where they landed on the sheet.

	DEFAULTS TO THE NEXT UNSIGNED ROW. For Supplement B that is the newest, which
	is what the alert was about; for a sheet it is the first one still blank,
	which is the order a room signs in. A caller who names a row that is already
	signed is refused rather than silently redirected — "I signed the wrong row"
	and "the row I meant was already done" are different mistakes.
	"""
	label = box.child_label
	rows = list(doc.get(box.child_table) or [])
	if not rows:
		raise ToolError(
			f"{doc.name} has no {label}s, so there is nothing on it to sign."
			+ (f" {box.child_hint}" if box.child_hint else "")
			+ " Nothing was changed."
		)

	wanted = (
		as_str(args, "row")
		or as_str(args, "reverification")
		or as_str(args, "child_row")
		or (as_str(args, box.child_subject_field) if box.child_subject_field else "")
	)
	if wanted:
		for row in rows:
			if str(row.get("name") or "") == wanted:
				return row
		if box.child_subject_field:
			# Resolved through the subject's own resolver, so a docname, an
			# employee number, a name or a login all find the row — the four
			# things somebody calls a person, and the same four `resolve_employee`
			# takes everywhere else on this surface.
			subject = _resolve_subject(box, wanted)
			for row in rows:
				if str(row.get(box.child_subject_field) or "") == subject:
					return row
		if wanted.isdigit() and 1 <= int(wanted) <= len(rows):
			return rows[int(wanted) - 1]
		raise ToolError(
			f"{doc.name} has no {label} {wanted!r}. Its rows are: "
			+ ", ".join(
				f"{index}. {row.get('name')} ({_row_caption(box, row)})"
				for index, row in enumerate(rows, start=1)
			)
			+ ". Pass the row docname"
			+ (f", the {box.child_subject_field}" if box.child_subject_field else "")
			+ " or its 1-based position. Nothing was changed."
		)

	unsigned = [row for row in rows if not str(row.get(box.field) or "").strip()]
	if not unsigned:
		raise AlreadySignedError(
			f"every {label} on {doc.name} already carries a signature. Name the row explicitly "
			f"with `row` and pass overwrite=true to replace one filed in error. Nothing was "
			f"changed."
		)
	if box.child_order_field:
		unsigned.sort(key=lambda row: str(row.get(box.child_order_field) or ""), reverse=box.child_order_desc)
	return unsigned[0]


def _resolve_subject(box: SignatureBox, value: str) -> str:
	"""One row's subject from whatever a caller called it. "" where it will not resolve.

	NEVER RAISES: a value that resolves to nobody falls through to the refusal
	above, which lists the rows that ARE on the sheet — a more useful answer than
	"no such employee" to somebody who is holding the right sheet and the wrong
	name for it.
	"""
	if box.child_subject_field != "employee":
		return value
	from . import employee as employee_tool

	try:
		return employee_tool.resolve_employee(value)
	except Exception:
		return ""


def _row_caption(box: SignatureBox, row) -> str:
	"""How one row is described when a refusal lists them all."""
	if box.child_subject_field:
		return str(
			row.get(f"{box.child_subject_field}_name") or row.get(box.child_subject_field) or "unnamed"
		)
	return str(row.get(box.child_order_field or "") or "undated")


# ── the image ───────────────────────────────────────────────────────────────
def _capture(box: SignatureBox, form: str, args: dict, target=None) -> tuple[str, bytes, str]:
	"""(file_docname, content, file_url) for the image, from base64 or a token.

	Returns bytes for the base64 path and an empty `content` for the token path,
	because the two produce a File differently: one has to be written and the
	other already exists and only has to be pointed at the right record.
	"""
	token = as_str(args, "file_token") or as_str(args, "file") or as_str(args, "file_url")
	raw = (
		as_str(args, "signature_base64")
		or as_str(args, "signature_image")
		or as_str(args, "signature")
		or as_str(args, "file_content")
	)
	if token and raw:
		raise ToolError(
			"both a base64 image and a file_token were given, and they are two different "
			"pictures as far as this call can tell. Send one. Nothing was changed."
		)
	if not (token or raw):
		raise ToolError(
			"the signature image is required: signature_base64 for a capture taken in the app "
			f"(up to {SIGNATURE_MAX_BYTES // 1024} KB, PNG or JPEG, no data: prefix), or "
			"file_token for one already uploaded with stage_file_chunk / finalize_staged_file. "
			"Nothing was changed."
		)

	if token:
		docname = token if frappe.db.exists("File", token) else ""
		if not docname and (token.startswith("/") or token.startswith("http")):
			docname = str(frappe.db.get_value("File", {"file_url": token}, "name") or "")
		if not docname:
			raise ToolError(
				f"no File called {token!r} on this site. Upload the capture first, or send it "
				f"as signature_base64. Nothing was changed."
			)
		row = frappe.db.get_value("File", docname, ["file_name", "file_url"], as_dict=True) or {}
		_check_extension(str(row.get("file_name") or token))
		return docname, b"", str(row.get("file_url") or "")

	# THE PREFIX IS STRIPPED RATHER THAN REFUSED. A browser canvas hands back
	# `data:image/png;base64,iVBOR…` and a phone hands back the tail of it; both
	# are the same picture, and refusing one of them would be this app being
	# precious about a comma.
	if raw.startswith("data:"):
		_, _, raw = raw.partition(",")
	content = files.decode_base64_content(raw, tail="Nothing was changed.")
	if len(content) > SIGNATURE_MAX_BYTES:
		raise ToolError(
			f"the capture is {len(content)} bytes and this endpoint takes signature captures "
			f"up to {SIGNATURE_MAX_BYTES} — a finger on a glass rectangle is a few kilobytes of "
			f"monochrome PNG. Something this size is a SCAN of a signed page, which goes up "
			f"through stage_file_chunk / finalize_staged_file and is filed with attach_signed_i9 "
			f"as the retained federal copy. Nothing was changed."
		)
	extension = _sniff(content)
	_check_extension(f"x{extension}")
	# THE SUBJECT IS IN THE NAME WHERE THE ROWS ARE PER-PERSON. A sign-in sheet
	# hangs twelve captures off one document, and twelve Files all called
	# `TRNS-2026-0001-signature.png` is a private files directory nobody can read
	# by eye — which is the whole reason the docname and the field are in it.
	stem = form
	if box.child_subject_field and target is not None:
		subject = str(target.get(box.child_subject_field) or "").strip()
		if subject:
			stem = f"{form}-{subject}"
	# `signature-signature.png` where the column is already called `signature`,
	# which is the training box and would be the next one too. The stem is there
	# to say what the file IS; a field that already says it does not need it said
	# twice.
	tail = box.field if box.field.endswith(FILE_STEM) else f"{box.field}-{FILE_STEM}"
	name = f"{stem}-{tail}{extension}"
	return "", content, name


def _sniff(content: bytes) -> str:
	"""`.png` or `.jpg` off the magic bytes, and a refusal for anything else.

	THE BYTES DECIDE, NOT A FILENAME THE CALLER CHOSE. This endpoint stores what
	it is sent under a name it composes itself, so there is no caller-supplied
	extension to trust — and a signature box holding an HTML document that a
	filename called `.png` is the exact confusion `api/files.py` keeps `.svg`
	off its allowlist to avoid.
	"""
	if content[:8] == b"\x89PNG\r\n\x1a\n":
		return ".png"
	if content[:3] == b"\xff\xd8\xff":
		return ".jpg"
	raise ToolError(
		"the capture is neither a PNG nor a JPEG — this app reads the first bytes rather than "
		"trusting a name, because a signature box holding something that merely ends in .png "
		"is worse than an empty one. Export the capture as PNG. Nothing was changed."
	)


def _check_extension(file_name: str) -> None:
	extension = ("." + file_name.rsplit(".", 1)[-1].lower()) if "." in file_name else ""
	if extension not in SIGNATURE_EXTENSIONS:
		raise ToolError(
			f"{file_name!r} is a {extension or 'file with no extension'} and a signature capture "
			f"is an image: {', '.join(SIGNATURE_EXTENSIONS)}. Nothing was changed."
		)


# ── the tool ────────────────────────────────────────────────────────────────
def collect_form_signature(args: dict) -> ToolResult:
	"""Attach one signature capture to the box that was missing it, and close the task.

	THE CALL A FARM TASK RAISED BY `i9_section_2_unsigned` IS ASKING FOR. The
	alert found the empty box, the task put it on somebody's phone, and this is
	what that phone calls when the signature has been drawn.

	WHAT IT REFUSES, AND WHY EACH REFUSAL IS BEFORE THE WRITE:

	  * a field that is not one of the five signature boxes, and a FORM that has
	    no signature line at all — an endpoint that wrote an image into any
	    column somebody named is an arbitrary write, and one that accepted a
	    signature for a W-2 would be inventing a box the IRS does not print;
	  * a caller who cannot WRITE the form, checked through Frappe's own
	    permission system rather than through a role list this app invented;
	  * a caller whose User Permissions do not name the ENTITY the form belongs
	    to — the same answer Frappe's check gives, asked again against the
	    `company` column so that a permission-cache failure cannot widen it into
	    "any I-9 on the site". See `_require_entity`;
	  * a caller not on the authorized-signer roster, for the two boxes it
	    governs — Section 1 and the W-4 are signed by the worker, who is on
	    nobody's roster and must not need to be, and the employer's tax returns
	    are signed by an officer this app keeps no register of;
	  * a box that already carries a signature, unless `overwrite` — replacing
	    an attestation silently is the one write here that could not be noticed
	    afterwards;
	  * a destroyed I-9, which is a record whose documents no longer exist;
	  * v0.60.0, a BADGE THAT RESOLVES TO SOMEBODY ELSE on a box the worker signs
	    in their own name, and a caller CLAIMING A CAPACITY the box contradicts.
	    Both are identity failures rather than permission ones, and an identity
	    check that fails open is not one — see `_identity` and `_evidence_role`.

	AND WHAT IT DOES NOT REFUSE: a task it could not close, a PDF it could not
	redraw, and an evidence row it could not write. All three are reported, none
	of them undoes the signature. See the module docstring — the capture is the
	compliance artefact and the rest is bookkeeping about it.
	"""
	box = _box(args)
	compat.require_doctype(
		box.doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)
	name = _form_name(box, args)

	doc = frappe.get_doc(box.doctype, name)
	if box.doctype == I9_FORM and str(doc.get("status") or "") == "Destroyed":
		raise ToolError(
			f"I-9 {name} was destroyed at the end of its retention period. A signature filed "
			f"against a destroyed record would attest to documents that no longer exist. "
			f"Nothing was changed."
		)

	# PERMISSION FIRST, AND THROUGH FRAPPE'S OWN CHECK. `write`, not `read`: a
	# signature changes what the document asserts about a person. Doing this
	# before anything is decoded means a caller who may not sign is refused
	# having had nothing of theirs stored.
	_require_write(box, name)
	company = str(doc.get("company") or "")
	signer = _require_signer(box, company)

	role = _evidence_role(box, args)

	# THE ROW BEFORE THE IDENTITY, because on a table of per-person rows the row
	# is what says who the signer is supposed to BE. A Training Session has no
	# `employee` column — twelve people sign one afternoon in their own names —
	# so an identity check made against the parent would have had no subject to
	# compare a badge against and would have passed everything. Both are still
	# refusals before any write, and both are still after the permission check,
	# so the only thing this ordering changes is which of two pre-write refusals
	# a caller sees first.
	target = doc
	child = None
	if box.child_table:
		child = _child_row(box, doc, args)
		target = child

	# THE CAPACITY AND THE IDENTITY, BEFORE ANY OF THE FOUR WRITES. A badge naming
	# the wrong person and a role the box contradicts are both refusals, and both
	# have to land while there is still nothing of this caller's on the site.
	identity = _identity(box, doc, role, args, target=target)

	# THE FINGERPRINT, TAKEN NOW. `doc` has not been touched yet, so this is the
	# record as the signer was shown it — which is the only moment at which the
	# question "what did they see" has an answer. See `hash_exclusions`.
	fingerprint = signing_evidence.document_fingerprint(doc, exclude=hash_exclusions(box.doctype))

	overwrite = as_bool(args, "overwrite", False)
	existing = str(target.get(box.field) or "").strip()
	if existing and not overwrite:
		raise AlreadySignedError(
			f"{box.label} on {name} already carries a signature at {existing}. That is an "
			f"attestation somebody made under penalty of perjury. Pass overwrite=true only to "
			f"replace one filed in error; the File that was there stays attached to the record "
			f"either way. Nothing was changed."
		)

	file_docname, content, file_name = _capture(box, name, args, target=target)

	# ── 1. the image ────────────────────────────────────────────────────
	if content:
		handle = files.insert_attachment(
			file_name,
			content,
			is_private=True,
			doctype=box.doctype,
			name=name,
			attached_to_field=box.field,
		)
	else:
		# THROUGH THE DOCUMENT, NOT `db.set_value`, for the reason
		# `i9.attach_signed_i9` gives: making a public File private MOVES the
		# bytes and rewrites the URL, and only the File controller does the
		# moving. Flipping the column would leave a URL that says private and
		# resolves public.
		handle = frappe.get_doc("File", file_docname)
		handle.attached_to_doctype = box.doctype
		handle.attached_to_name = name
		handle.attached_to_field = box.field
		handle.is_private = 1
		handle.flags.ignore_permissions = True
		handle.save()
	stored = str(handle.get("file_url") or "")

	# ── 2. the form ─────────────────────────────────────────────────────
	_write(target, box.field, stored)
	_write(target, box.signed_at_field, frappe.utils.now())
	if box.signed_ip_field:
		_write(target, box.signed_ip_field, _remote_addr())
	# WRITTEN ONLY WHERE THERE IS A FIX, not blanked where there is not. A pad
	# that reported coordinates last time and cannot get a lock this time is a
	# handset under a tree, and clearing the column would replace a location that
	# was recorded with one that was not — the same overwrite the printed-name
	# rule below refuses. `as_gps` returns "" for a missing pair and for (0, 0).
	if box.signed_gps_field:
		fix = as_gps(args)
		if fix:
			_write(target, box.signed_gps_field, fix)
	# The printed name goes on ONLY where the roster supplied one and the box has
	# nothing in it. A form that already names its verifier is a form where
	# somebody examined the documents and this call is finishing their signature,
	# not re-attributing their examination.
	if signer.get("full_name") and box.name_field and not str(target.get(box.name_field) or "").strip():
		_write(target, box.name_field, signer["full_name"])
		if box.title_field and signer.get("title"):
			_write(target, box.title_field, signer["title"])
	doc.flags.ignore_permissions = True
	doc.save()

	# v0.64.2. AND THE FORM COMPLETES ITSELF WHEN THE LAST BOX IS FILLED.
	# `submit_i9_section_2` no longer marks a form Complete with a blank
	# attestation on it — it files the documents and rests at Awaiting
	# Verification — so something has to notice when the outstanding signature
	# arrives, and the signature landing IS that moment. It moves ONE edge and
	# never raises; see `i9.advance_if_signed`.
	advanced = i9.advance_if_signed(name) if box.doctype == I9_FORM else ""

	if box.doctype == I9_FORM:
		# `Signature Collected` was not a valid `action` on I-9 Audit Log until
		# v0.138.0, so this row has never been written either — same swallow, same
		# missing Select option as the `Completed` row `i9.advance_if_signed` writes
		# one line up. Found together and fixed together: a capture on a federal
		# form left no audit trail at all, which is the one thing the log exists for.
		i9._log_action(
			name,
			str(doc.get("employee") or ""),
			"Signature Collected",
			{
				"field": box.field,
				"row": str(child.get("name")) if child is not None else None,
				"file": stored,
				"file_docname": handle.name,
				"replaced": existing or None,
				"signer": signer.get("full_name") or None,
			},
		)

	result = {
		"doctype": box.doctype,
		"name": name,
		"field": box.field,
		"label": box.label,
		"row": str(child.get("name")) if child is not None else None,
		"employee": doc.get("employee"),
		"employee_name": doc.get("employee_name"),
		"signature": stored,
		"file_docname": handle.name,
		"signed_at": str(target.get(box.signed_at_field) or ""),
		"replaced": existing or None,
		"signer_roster_enforced": bool(signer.get("configured")),
		# v0.64.2. The status this signature moved the form to, or null where it
		# moved nothing — which is every signature except the one that fills the
		# last outstanding attestation on a form whose Section 2 is already filed.
		"form_status_advanced_to": advanced or None,
		# ── THE ALERT THIS ANSWERED, READ BEFORE ANYTHING SWEEPS ────────────
		#
		# v0.64.1, and the ordering is the entire point. `alert_answered_by`
		# finds the OPEN alert this signature makes untrue, and two steps below
		# this line now make it stop being open — `_close_the_task` completes the
		# Farm Task, whose completion re-runs the rule, and
		# `_evaluate_compliance_after` re-runs it directly. Read afterwards, the
		# lookup finds a dismissed row and answers "" — so the app's compliance
		# tab would be told nothing was answered on precisely the calls where the
		# work landed and the alert cleared, which is backwards.
		#
		# It is still NOT a claim that an alert was dismissed; see
		# `alert_answered_by`. It names the row this signature answered, at the
		# moment it answered it, and whether the sweep then agreed is
		# `compliance_evaluation`'s business to report.
		"answered_alert": alert_answered_by(box, name) or None,
	}

	# ── 3. the evidence ─────────────────────────────────────────────────
	result["evidence"] = _record_evidence(
		box=box,
		doc=doc,
		target=target,
		role=role,
		identity=identity,
		signer=signer,
		fingerprint=fingerprint,
		company=company,
		stored=stored,
		replaced=bool(existing),
		args=args,
	)

	# ── 4. the task, and ── 5. the PDF ──────────────────────────────────
	result["task"] = _close_the_task(box, name, handle.name, args)
	result["pdf"] = _redraw(box, name, ensure=as_bool(args, "render_pdf", False))
	result["compliance_evaluation"] = _evaluate_compliance_after(box, company)

	return ToolResult(
		data=result,
		summary=f"{box.label} signed on {name}"
		+ (f" for {doc.get('employee_name')}" if doc.get("employee_name") else "")
		+ (f", task {result['task']['task']} completed" if (result["task"] or {}).get("completed") else ""),
	)


def _record_evidence(
	*,
	box: SignatureBox,
	doc,
	target,
	role: str,
	identity: dict,
	signer: dict,
	fingerprint: dict,
	company: str,
	stored: str,
	replaced: bool,
	args: dict,
) -> dict:
	"""Write the Signing Evidence row for the signature that has just landed.

	AFTER THE FORM, NOT BEFORE IT, and the ordering is the module docstring's
	rather than a convenience. An evidence row written first would describe a
	signature that might not exist; written here it describes one that does. The
	step never raises — `signing_evidence.record` swallows and reports — for the
	reason the task and the PDF never do.

	`signed_at` COMES OFF THE FORM RATHER THAN OFF THE CLOCK. The column was
	stamped a few lines above and the evidence row must say the same moment as
	the record it is evidence about; reading `now()` a second time would put two
	timestamps a millisecond apart on one signature, which is the kind of
	discrepancy an auditor is trained to ask about and nobody can explain.

	`supersedes` IS ONLY LOOKED UP WHEN SOMETHING WAS REPLACED, because that is
	the only time the answer means anything. A first signature on a box that a
	previous evidence row happens to mention would otherwise claim to supersede a
	record it has nothing to do with.
	"""
	subject, subject_name = _subject(box, doc, target)
	who = identity.get("employee") or (subject if role == "Employee" else "")
	who_name = identity.get("employee_name") or (subject_name if who and who == subject else "")
	context = _context(args)
	return signing_evidence.record(
		document_type=box.doctype,
		document_name=doc.name,
		signature_role=role,
		signature_field=box.field,
		signed_at=str(target.get(box.signed_at_field) or frappe.utils.now()),
		company=company,
		signer=who,
		signer_name=who_name or signer.get("full_name") or "",
		# THE HUMAN, NOT THE EFFECTIVE USER. `mcp.handle` switches to the MCP
		# System User a line after it authenticates, so `frappe.session.user`
		# would name the same account for all forty callers — see
		# `signers._current_user`, which reads back the identity captured before
		# that switch. On an evidence row that distinction is the whole column.
		signer_user=signers._current_user(),
		signer_badge=identity.get("badge") or "",
		verification_method=identity.get("method") or "",
		signature_image=stored,
		document_hash=fingerprint["hash"],
		hashed_fields=",".join(fingerprint["fields"]),
		device_id=context["device_id"],
		ip_address=context["ip_address"],
		gps_latitude=context["latitude"],
		gps_longitude=context["longitude"],
		supersedes=(signing_evidence.supersede_target(box.doctype, doc.name, box.field) if replaced else ""),
	)


def _write(target, fieldname: str, value) -> None:
	"""Set one column on a document OR on a child row.

	A CHILD ROW IS NOT ALWAYS A DOCUMENT. Frappe hands back child tables as
	Documents most of the time and as plain dicts some of the time — a row read
	straight off `get_all`, a row on a doctype nothing has rehydrated — and the
	only write this module makes to a child is this one. `doc.set` on a dict is
	an AttributeError in the middle of storing a signature, which is the worst
	possible place for one.
	"""
	setter = getattr(target, "set", None)
	if callable(setter):
		setter(fieldname, value)
	else:
		target[fieldname] = value


def _remote_addr() -> str:
	return (
		frappe.local.request.remote_addr
		if hasattr(frappe, "local") and hasattr(frappe.local, "request") and frappe.local.request
		else ""
	)


def _require_write(box: SignatureBox, name: str) -> None:
	"""Refuse unless the acting account may modify the form being signed.

	FRAPPE'S OWN CHECK, not a role list compiled into this app. Who may write an
	I-9 on a given site is a question that site's permission rules answer — a
	role, a user permission on a company, a share — and reimplementing it here
	would be a second answer that drifts from the Desk's.
	"""
	try:
		permitted = frappe.has_permission(box.doctype, "write", doc=name)
	except Exception:  # pragma: no cover - a site whose permission cache is cold
		permitted = frappe.has_permission(box.doctype, "write")
	if not permitted:
		raise ToolError(
			f"this account may not write {box.doctype} {name}, so it may not put a signature on "
			f"it. A signature changes what the form asserts about a person, which is why the "
			f"check is `write` and not `read`. Nothing was changed."
		)
	_require_entity(box, name)


def _require_entity(box: SignatureBox, name: str) -> None:
	"""Refuse a form belonging to an entity this account is not scoped to.

	v0.59.3. FRAPPE ALREADY ANSWERS THIS AND THIS ASKS AGAIN ANYWAY, which needs
	the argument spelled out because the function above exists to say the
	opposite. `has_permission(..., doc=name)` applies the caller's Company User
	Permissions to every Link on the record, so a Farm Manager scoped to one
	entity is already refused another's I-9 on the line before. What is asked
	again here is the ONE question whose fallback drops it: the `except` branch
	above answers a permission-cache failure with a DOCTYPE-level check, and a
	doctype-level check knows nothing about which company the record belongs to.
	Before the six roles held `write` on any federal form that branch was
	harmless — it fell through to a refusal either way. Now that a Farm Manager
	holds `write` on I-9 Form it is the difference between "may this role sign
	an I-9" and "may this manager sign THIS I-9", and those are not the same
	question.

	SO THE ENTITY IS CHECKED AGAINST THE COLUMN, not against a permission cache
	that may be the thing that just failed. `Company` is a required Link on all
	three signable forms, which is what makes one line of SQL a complete answer.

	AN ACCOUNT WITH NO USER PERMISSION IS UNRESTRICTED, deliberately, and it is
	the same disagreement `permissions.py` documents at length: that is Frappe's
	rule, it is what makes an operator's own login and the MCP System User work,
	and departing from it here would refuse every correctly-configured operator
	on a tool they have always been able to call. The strict reading lives on the
	mobile door — `api/guard.require_scope` refuses a phone with no entities
	outright, and `create_mobile_user` will not make such an account — so the
	surface facing the open internet is the one that fails closed.
	"""
	try:
		allowed = roles.companies_for(_current_user())
		if not allowed:
			return
		company = str(frappe.db.get_value(box.doctype, name, "company") or "")
	except Exception:  # pragma: no cover - a site whose User Permission table we cannot read
		return
	if company and company not in allowed:
		raise ToolError(
			f"{box.doctype} {name} belongs to {company}, which this account is not scoped to. "
			f"A signature is an attestation about one employer's record, and the entities an "
			f"account may attest for are the ones its User Permissions name — an operator "
			f"changes that with create_mobile_user(update_existing=true). Nothing was changed."
		)


def _require_signer(box: SignatureBox, company: str = "") -> dict:
	"""The roster row authorising this account, `{}` where the box is not gated.

	THE EMPLOYEE BOXES ARE NOT GATED, and that is the point of `form_type` being
	empty on three of the five. Section 1 and the W-4 are signed by the worker
	themselves under their own penalty of perjury; requiring the account holding
	the phone to be an authorized signer would mean the only people who could
	collect a worker's signature are the people authorised to sign FOR the
	employer, which is precisely the conflation §274a keeps apart.

	v0.60.0 PASSES THE COMPANY THROUGH, and the reason is that "authorized to
	sign" was only ever half a question. The roster answers WHICH FORM somebody
	may sign; the entity whose record they are signing is a separate fact that
	lives in Frappe's User Permissions, and until this release the two were
	checked in two places that did not know about each other — so a refusal could
	say "you are not authorized for I-9" when the truth was "not for this
	employer's I-9", which sends an operator to the wrong register.
	"""
	if not box.form_type:
		return {}
	return signers.authorized_signer_for_company(_current_user(), box.form_type, company)


# ── v0.60.0: the capacity, and who is standing at the pad ───────────────────
def _evidence_role(box: SignatureBox, args: dict) -> str:
	"""The capacity this signature is made in. THE BOX DECIDES.

	A caller may state a role and it is CHECKED; it is never believed over the
	table. Section 1 of a Form I-9 is a worker attesting under their own penalty
	of perjury and Section 2 is the employer attesting that it examined that
	worker's documents — two different legal acts on one piece of paper, and a
	client that could label them would be choosing which of the two it had just
	performed. The refusal names both, because a phone sending the wrong one is
	almost always a phone that opened the wrong pad.
	"""
	from_box = signing_evidence.ROLES_BY_BOX.get(box.signer_role or "", "")
	asked = signing_evidence.normalise_signature_role(
		as_str(args, "signature_role") or as_str(args, "signer_role")
	)
	if asked and from_box and asked != from_box:
		raise ToolError(
			f"{box.label} is signed in the capacity of {from_box}, and this call says "
			f"{asked}. The capacity is a property of the box rather than of the caller — "
			f"an employer's attestation filed as an employee's, or the reverse, is a false "
			f"statement about who examined what. Open the pad for the box you mean. "
			f"Nothing was changed."
		)
	return from_box or asked


def _subject(box: SignatureBox, doc, target=None) -> tuple:
	"""`(employee, employee_name)` this box is signed BY, on this record.

	THE ROW WINS WHERE THERE IS ONE. Five of the six boxes are on a form about one
	person and `doc.employee` is that person; the sixth is a sheet whose rows are
	each about a different one, and reading the parent there would compare a
	picker's badge against an empty column — an identity check that passes
	everything, which is worse than none because it looks like one.
	"""
	if box.child_subject_field and target is not None:
		return (
			str(target.get(box.child_subject_field) or ""),
			str(target.get(f"{box.child_subject_field}_name") or ""),
		)
	return str(doc.get("employee") or ""), str(doc.get("employee_name") or "")


def _identity(box: SignatureBox, doc, role: str, args: dict, target=None) -> dict:
	"""Who was proved to be at the pad, or the refusal that stops the call.

	THE STEP THAT MAKES A SIGNATURE WORTH SOMETHING, and it is the one this app
	did not have. A drawn shape plus a timestamp proves that somebody was holding
	a phone; a badge scanned at the moment of signing, resolved on the server
	against this employer's own register, proves WHICH somebody.

	THE BADGE IS RESOLVED THROUGH `resolve_badge`, NOT READ OFF THE TABLE HERE.
	That tool already refuses an unknown card, a retired one, one belonging to
	another entity and one whose holder has left — four sentences an operator can
	act on, and four ways an identity check can be quietly wrong if it is written
	a second time.

	A BADGE THAT RESOLVES TO THE WRONG PERSON IS FATAL, and only on the boxes
	where the signer is supposed to be the form's own employee. Section 1 and the
	W-4 are the worker signing for themselves, so a badge naming somebody else is
	either the wrong worker at the pad or the wrong form open — both of which end
	the call. Section 2 is the EMPLOYER's representative, whose badge is
	legitimately not the employee's; there the scan identifies the verifier and is
	checked against the roster instead, which `_require_signer` has already done.

	NO BADGE IS NOT AN ERROR. An operator signing a 941 at a desk has no card to
	scan, and refusing them would be this app inventing a requirement no form
	makes. The row says `Unverified` and an auditor is told which kind of packet
	they are holding.
	"""
	badge_id = as_str(args, "signer_badge") or as_str(args, "badge_id") or as_str(args, "badge")
	method = signing_evidence.normalise_verification_method(as_str(args, "verification_method"))
	subject, subject_name = _subject(box, doc, target)
	out = {"badge": "", "method": method, "employee": "", "employee_name": ""}

	if not badge_id:
		if method == "Badge QR":
			raise ToolError(
				"verification_method says a badge was scanned and no signer_badge came with it. "
				"An identity check the server cannot repeat is not one it may record — the column "
				"would look like proof and hold nothing. Send the badge, or omit the method. "
				"Nothing was changed."
			)
		# `Photo` and `Employee ID` are attested by the client and are recorded as
		# such. They are weaker than a badge and they are not nothing; what would
		# be nothing is a method with no subject at all, so the person the form is
		# about stands in where the box is one they sign themselves.
		if method and role == "Employee" and subject:
			out["employee"] = subject
			out["employee_name"] = subject_name
		return out

	resolved = badges.resolve_badge({"badge_id": badge_id, "company": str(doc.get("company") or "")}).data
	holder = str(resolved.get("employee") or "")
	if role == "Employee" and subject and holder != subject:
		raise ToolError(
			f"badge {badge_id!r} belongs to {holder} ({resolved.get('employee_name')}), and "
			f"{box.label} on {doc.name} is signed by {subject} "
			f"({subject_name or 'the person named on the row'}) in their own name. "
			f"Either the wrong person is at the pad or the wrong form is open, and a signature "
			f"filed across that gap would attest under one person's penalty of perjury to another "
			f"person's document. Nothing was changed."
		)
	out.update(
		{
			"badge": str(resolved.get("badge_id") or badge_id),
			"method": method or "Badge QR",
			"employee": holder,
			"employee_name": str(resolved.get("employee_name") or ""),
		}
	)
	return out


def _context(args: dict) -> dict:
	"""Where and on what the signature was drawn, as the client reports it.

	CORROBORATION, NOT AUTHENTICATION, and the distinction is why none of these
	is checked against anything. A device UUID and a pair of coordinates are what
	the handset says about itself; the server cannot verify either, and treating
	an unverifiable claim as a verified one is how a record comes to assert more
	than it knows. What they are worth is the ordinary thing corroboration is
	worth — a signature drawn on a known device at the packing shed, and one drawn
	somewhere else, are different facts to have written down.

	COORDINATES ARE ALL-OR-NOTHING. A latitude with no longitude is a point on a
	line rather than a place, and half a fix recorded as if it were a whole one is
	worse than none.
	"""
	latitude = args.get("gps_latitude", args.get("gps_lat"))
	longitude = args.get("gps_longitude", args.get("gps_lon"))
	fix = {"latitude": None, "longitude": None}
	if latitude not in (None, "") and longitude not in (None, ""):
		fix = {
			"latitude": as_float(latitude, "gps_latitude"),
			"longitude": as_float(longitude, "gps_longitude"),
		}
	return {
		"device_id": as_str(args, "device_id") or as_str(args, "device"),
		"ip_address": _remote_addr(),
		**fix,
	}


def _current_user() -> str:
	try:
		return str(frappe.session.user or "")
	except Exception:  # pragma: no cover - no session, as in a scheduled run
		return ""


def _close_the_task(box: SignatureBox, form: str, file_docname: str, args: dict) -> dict:
	"""Complete the Farm Task that asked for this signature, where there is one.

	NEVER RAISES, AND NEVER FORCES A COMPLETION. It delegates to
	`complete_farm_task`, which refuses a completion filed by somebody who was
	not holding the task — "a completion filed by somebody who was not there is
	not a chain of custody, it is a rumour". That refusal is the point of the
	doctype and this is not the place to route around it: an office account that
	collected a signature on behalf of the foreman holding the task has done
	something real, and what should happen is that the FOREMAN closes their own
	task, not that the office closes it in their name.

	So the return is a report rather than an outcome: which task was found, and
	whether it closed or why it did not.
	"""
	if not compat.doctype_exists(FARM_TASK):
		return {}
	from . import dispatch

	task = as_str(args, "task")
	if not task:
		task = _task_for(box, form)
	if not task:
		return {}

	row = frappe.db.get_value(FARM_TASK, task, ["name", "state", "assigned_to"], as_dict=True) or {}
	if not row:
		return {"task": task, "completed": False, "note": f"no Farm Task called {task!r} on this site."}
	if str(row.get("state") or "") in ("Completed", "Cancelled"):
		return {"task": task, "completed": False, "note": f"already {row.get('state')}."}

	try:
		dispatch.complete_farm_task(
			{
				"task": task,
				"worker_id": row.get("assigned_to"),
				"signature_file": file_docname,
				"findings_text": "",
				"completion_narrative": f"{box.label} collected and attached to {form}.",
			}
		)
	except Exception as exc:
		return {
			"task": task,
			"completed": False,
			"note": (
				f"the signature was filed and the task was left open ({exc}). Close it from the "
				"account holding it — a completion filed by somebody who was not there is not a "
				"chain of custody."
			),
		}
	return {"task": task, "completed": True}


def _evaluate_compliance_after(box: SignatureBox, company: str) -> dict | None:
	"""Re-run the rules this signature could have made untrue. NEVER RAISES.

	v0.64.1, and it closes the second half of the hole v0.64.0 opened the first
	half of. `complete_farm_task` re-runs the rules a completion could have
	changed at the moment it files, so the alert is gone by the time the phone
	reads the answer. A SIGNATURE reached that only sideways: `_close_the_task`
	completes the Farm Task the sweep raised, and the completion sweeps. Where
	there is no such task — nobody has run `generate_tasks_from_compliance_alerts`,
	or the task was closed by hand, or the form was signed from the compliance tab
	rather than from the board — nothing swept, and the row the worker had just
	tapped came straight back on the next load and sat there for up to an hour.

	THE NARROWING IS THE BOX'S OWN `alert_types`. It is the same tuple
	`_task_for` matches a task on and `alert_answered_by` computes a docname
	from — the rules that fire on THIS blank box, stated once — so a sixth
	signature box added to `SIGNATURE_BOXES` gets this for nothing and cannot be
	narrowed wrongly.

	IT IS THE SWEEP, CALLED SOONER. Nothing here dismisses an alert because a
	signature landed; the rule re-runs and reads the column, which now has a URL
	in it. A box filled on a form whose OTHER box is still blank leaves that
	other alert standing, which is correct and is why the rule decides rather
	than this function.

	RUNNING TWICE IS HARMLESS AND IS THE COMMON CASE. Where a task did close, its
	completion has already swept the same rule; the sweep is idempotent by
	construction — `alert_key` carries nothing that changes — so the second pass
	finds the row the first one wrote and refreshes it.

	NEVER RAISES AND NEVER UNDOES THE SIGNATURE, which is the ordering the module
	docstring sets out: the capture is the irreplaceable artefact and every step
	after it is bookkeeping about it. A rule that throws, a site mid-migrate, an
	alert table that is not there yet — none of them is a reason to refuse ink
	that is already on a federal form.
	"""
	names = sorted({str(key).strip() for key in (box.alert_types or ()) if str(key).strip()})
	if not names or not compat.doctype_exists(ALERT):
		return None
	try:
		from ..alerts import base as alerts_base

		report = alerts_base.refresh_compliance_alerts(company=company or "", alert_types=names)
	except Exception as exc:
		return {
			"rules_asked": names,
			"evaluated": False,
			"why_not": (
				f"the narrowed sweep did not run: {type(exc).__name__}: {exc}. The signature, its "
				"evidence row and the form it is on are unaffected, and the scheduled sweep will "
				"reach these rules on its next pass."
			),
		}
	dismissed = [
		entry["name"] for entry in report.get("alerts") or [] if entry.get("outcome") == "auto_dismissed"
	]
	out = {
		"rules_asked": names,
		"evaluated": True,
		"auto_dismissed": dismissed,
		"created": report.get("created", 0),
		"refreshed": report.get("refreshed", 0),
		"reopened": report.get("reopened", 0),
		"note": (
			"These are the rules that fire on this signature box. THE SWEEP DECIDED, not the "
			"signature: an alert here went away because its rule looked again and found the box "
			"filled, which is the only honest way one goes away."
		),
	}
	if not dismissed:
		out["standing_note"] = (
			"No alert dismissed. That is a normal outcome: a form with two blank boxes has two "
			"alerts, and filling one leaves the other standing until somebody signs it."
		)
	return out


def _task_for(box: SignatureBox, form: str) -> str:
	"""The open Farm Task raised for this exact box, or "".

	MATCHED ON THE SUBJECT LINK AND THE ALERT TYPE, both. The link alone would
	find a task raised for a DIFFERENT box on the same form — an I-9 missing both
	signatures has two tasks, held by two different people — and closing the
	wrong one would tell a supervisor the employee had signed because an
	authorized signer had.
	"""
	if not (compat.has_field(FARM_TASK, "subject_docname") and box.alert_types):
		return ""
	rows = frappe.db.get_all(
		FARM_TASK,
		filters={
			"subject_doctype": box.doctype,
			"subject_docname": form,
			"state": ("not in", ("Completed", "Cancelled")),
		},
		fields=["name", "source_alert"],
		order_by="creation desc",
		limit=20,
	)
	for row in rows or []:
		alert_type = str(frappe.db.get_value("Compliance Alert", row.get("source_alert"), "alert_type") or "")
		if alert_type in box.alert_types:
			return str(row["name"])
	return ""


def _redraw(box: SignatureBox, form: str, ensure: bool = False) -> dict:
	"""Regenerate the form's PDF so the retained page carries the new signature.

	THE WHOLE REASON THIS STEP IS HERE. `render_i9_pdf` stamps captured
	signatures into the page content and flattens it, so the generated PDF is
	the page an inspection is shown — and a PDF rendered before the signature
	arrived is a page with an empty box on a form that is signed. Leaving it
	stale would mean the record and its printable copy disagree about the one
	fact somebody would print it to prove.

	REGENERATION, AND ONLY REGENERATION, UNLESS THE CALLER ASKED. A form that has
	never been rendered gets nothing by default: `render_i9_pdf` is one call
	away, it is the caller's decision when to make it, and producing a federal
	form nobody asked for — on a phone, in an orchard, as a side effect of
	collecting a signature — is this app deciding something that is not its to
	decide. What it will not leave behind is a rendered page that has gone stale,
	which is why `overwrite` is passed: the File that was there stays attached to
	the record either way, so the printed copy somebody already had signed is not
	lost.

	`ensure=True` IS THAT DECISION BEING MADE, NOT ROUTED AROUND. v0.57.1 added
	it for the signature pad, which asks for the page back so the person who just
	signed can see what they signed. That is a caller asking by name, in the same
	call, for a specific reason — which is exactly the condition the paragraph
	above says is missing when the render happens as a side effect. Nothing
	changes for a caller that does not pass it.

	NEVER RAISES. The renderers need `pypdf` and the blank federal form on disk,
	and a site missing either has a working signature and no PDF — which is a
	smaller problem than a signature this call refused to store.
	"""
	handler = FORM_HANDLERS[box.doctype]
	try:
		existing = str(frappe.db.get_value(box.doctype, form, handler["pdf_field"]) or "").strip()
	except Exception:  # pragma: no cover - a site whose column is not migrated
		existing = ""
	if not existing and not ensure:
		return {
			"regenerated": False,
			"note": (
				"no PDF had been rendered for this form, so there was nothing to bring up to "
				f"date. {handler['renderer']} draws one, with the signature stamped in."
			),
		}
	try:
		result = handler["render"](form)
	except Exception as exc:
		return {
			"regenerated": False,
			"note": (
				f"the signature is on the record and the PDF was {'not drawn' if not existing else 'not redrawn'} "
				f"({exc}). "
				+ (
					f"The rendered page at {existing} is now out of date with the record it was drawn from; "
					if existing
					else "There is no rendered page; "
				)
				+ "nothing about the signature depends on it."
			),
		}
	data = getattr(result, "data", None) or {}
	return {
		"regenerated": True,
		"file_url": data.get("file_url"),
		"file_name": data.get("file_name"),
		"replaced": existing or None,
	}


# ── v0.57.0: what an alert is asking for, addressed ─────────────────────────
def request_for_alert(row: dict) -> dict | None:
	"""The `API_CONTRACT.md` §14.1 signature request one alert names, or None.

	THE COMPLIANCE CALENDAR WAS A NOTICEBOARD AND THIS IS WHAT MAKES A ROW A TAP.
	"I-9 Section 1 was completed but carries no employee signature — Critical" is
	a sentence a foreman can read and not act on: the pad that fixes it lives
	behind another tab, findable only by knowing which Farm Task the sweep raised
	and that it was a signature task at all. An alert that carries the ADDRESS —
	which doctype, which record, which column — opens the pad itself.

	DERIVED AT READ TIME RATHER THAN STORED ON THE ALERT, and there are three
	reasons, in ascending order of how much they matter:

	  * an alert raised by v0.55.0 and still open gets its address the moment the
	    site upgrades, with no patch and no sweep. Nothing about a row on this
	    calendar changes;
	  * the address is already a fact about the rule and the record, and a copy
	    on the alert is a second one to keep in step. `alert_key` is built from
	    exactly the pair this reads back;
	  * it comes off `SIGNATURE_BOXES`, which is the table the WRITE path gates
	    on. A pad can only be opened at a column `collect_form_signature` would
	    accept ink into — the alternative is a request addressed at a box this
	    module refuses, which collects a signature into nothing and asks for it
	    again wherever the form really lives.

	RETURNS None FOR EVERY OTHER ALERT, which is nearly all of them. An overdue
	housing inspection is not a missing signature and must not grow a pad.
	"""
	box = BOXES_BY_ALERT_TYPE.get(str(row.get("alert_type") or "").strip())
	if box is None:
		return None
	docname = str(row.get("source_docname") or "").strip()
	source_doctype = str(row.get("source_doctype") or "").strip()
	# THE SOURCE HAS TO BE THE FORM. A rule retargeted at a different doctype
	# without its box being updated would otherwise address a pad at a record of
	# the wrong kind — and the app treats an address as opaque, so nothing
	# downstream would catch it.
	if not docname or (source_doctype and source_doctype != box.doctype):
		return None

	request = {
		"doctype": box.doctype,
		"docname": docname,
		"signature_field": box.field,
		"signer_role": box.signer_role or None,
		"signer_label": box.signer_label or None,
		"form_label": box.form_label or None,
		"section_label": box.section_label or None,
		"attestation": box.attestation or None,
		"due_date": str(row.get("due_date") or "") or None,
	}
	request.update(_subject_of(box, docname))
	return {key: value for key, value in request.items() if value is not None}


def _subject_of(box: SignatureBox, docname: str) -> dict:
	"""Who the form is for and whose name belongs under the ruled line.

	NEVER RAISES AND NEVER GUESSES. A record the calendar can see and this
	account cannot read, a doctype without an `employee` column — a Tax Form is
	the employer's own return and has none — and a form deleted between the sweep
	and the read all produce the same answer, which is a request with an address
	and no biography. The app falls back to the doctype and drops the line.

	`printed_name` FOLLOWS THE ROLE RATHER THAN THE FORM. On Section 1 the name
	under the line is the employee's, because they are the one attesting. On
	Section 2 it is the VERIFIER's, and the employee's name there would be a
	claim about who examined the documents — so it comes off the record's own
	verifier column and is simply absent until somebody is named.
	"""
	fields = compat.existing_fields(
		box.doctype,
		[name for name in ("employee", "employee_name", box.name_field, box.form_label_field) if name],
	)
	if not fields:
		return {}
	try:
		row = frappe.db.get_value(box.doctype, docname, fields, as_dict=True) or {}
	except Exception:  # pragma: no cover - a record this account cannot read
		return {}

	out = {
		"employee": str(row.get("employee") or "") or None,
		"employee_name": str(row.get("employee_name") or "") or None,
	}
	if box.signer_role == "employee":
		out["printed_name"] = out["employee_name"]
	elif box.name_field:
		out["printed_name"] = str(row.get(box.name_field) or "") or None
	if box.form_label_field and row.get(box.form_label_field):
		out["form_label"] = f"Form {row[box.form_label_field]}"
	return {key: value for key, value in out.items() if value is not None}


def alert_answered_by(box: SignatureBox, form: str) -> str:
	"""The open Compliance Alert this signature has just made untrue, or "".

	NOT A CLAIM THAT AN ALERT WAS DISMISSED, and the distinction is the same one
	`api/shape.completion` makes at length: nothing here dismisses an alert. The
	sweep does that, by looking at the record again and finding the box filled,
	which is the only honest way for an alert to go away. What this names is the
	row the work ANSWERED — which is what a phone means when it takes the item
	off the compliance tab it was tapped from.

	FOUND BY KEY RATHER THAN BY SEARCH. An alert's docname is
	`alert_key(rule, doctype, docname)` and carries nothing that changes daily,
	so the alert a signature answers is computable rather than queryable. Never
	raises: a site mid-migrate has no alert table and a signature that landed is
	still a signature that landed.
	"""
	if not compat.doctype_exists(ALERT):
		return ""
	from .. import alerts as alert_engine

	for alert_type in box.alert_types:
		key = alert_engine.alert_key(alert_type, box.doctype, form)
		try:
			row = frappe.db.get_value(ALERT, key, ["name", "dismissed"], as_dict=True)
		except Exception:  # pragma: no cover - a site mid-migrate
			return ""
		if row and not frappe.utils.cint(row.get("dismissed")):
			return str(row["name"])
	return ""


__all__ = [
	"BOXES_BY_ALERT_TYPE",
	"BOXES_BY_KEY",
	"SIGNATURE_BOXES",
	"SIGNATURE_MAX_BYTES",
	"AlreadySignedError",
	"SignatureBox",
	"alert_answered_by",
	"collect_form_signature",
	"hash_exclusions",
	"request_for_alert",
]
