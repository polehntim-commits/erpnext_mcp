# SPDX-License-Identifier: MIT
"""What a scanned document has to survive before anybody believes it. Pure
functions, no database, no network, no model call.

────────────────────────────────────────────────────────────────────────────
THE THREE-STAGE PIPELINE, AND WHICH STAGE THIS FILE IS
────────────────────────────────────────────────────────────────────────────

A pesticide label photographed at a chemical shed goes through three stages,
and they are three stages precisely because each one is wrong in a way the
next one can catch:

  1. ON-DEVICE OCR AND EXTRACTION. Vision framework on the phone reads the
     label and pulls out fields — `rei_hours`, `phi_days`, an EPA registration
     number. It is fast, it is offline, and it CANNOT TELL WHETHER WHAT IT READ
     IS TRUE. `0` and `O` are the same shape at 200 dpi in a dusty shed.

  2. DETERMINISTIC VALIDATION — THIS FILE. Everything checkable without
     judgement: is that EPA number the shape an EPA number is, does the REI the
     phone read match what the label for that active ingredient actually
     carries, does the number appear in the OCR text at all, is a PHI of two
     days compatible with an REI of seventy-two hours (it is not — you cannot
     harvest a block you may not walk into).

  3. LLM ASSESSMENT. Judgement: does this read like a real label, is the
     product plausible for the crop, is anything about the document odd in a
     way no rule anticipated. See `merge_llm_assessment` and the section below
     on where that answer comes from.

Stage 2 exists because stage 3 is expensive, unavailable offline, and — this
is the part worth saying plainly — WORSE THAN A REGEX AT REGEX-SHAPED WORK. A
model asked whether `524-537` is a well-formed EPA registration number will
usually say yes and occasionally say yes about `S24-S37`. `EPA_REG_PATTERN`
never does.

────────────────────────────────────────────────────────────────────────────
WHERE THE LLM ANSWER COMES FROM, WHICH IS NOT FROM HERE
────────────────────────────────────────────────────────────────────────────

THERE IS NO MODEL CALL ANYWHERE IN THIS APP, and this module does not add the
first one. `proposals.py` states the architecture at length and it is the same
architecture here: THE AI IS THE MCP CLIENT. A model reading a scanned label
calls `validate_document_extraction`, and it may hand its own assessment along
in the same call — `llm_assessment`, shaped `{status, issues, confidence,
reasoning}`. What the tool does is what a tool can do and a model cannot do for
itself: run the checks a model is bad at, refuse the assessment if it is the
wrong shape, and record which model said it.

So `validate_extraction` is complete on its own and answers `Pending` when
nobody supplied an assessment — the honest status for "checked as far as rules
go, nothing has judged it". A site with no model in the loop still gets every
deterministic check, still gets a stored record, and still gets told, in an
issue with a code and a message rather than in a silence, that the judgement
half did not run.

ONE CARVE-OUT, AND IT IS DELIBERATE. A deterministic ERROR flags the document
whether or not a model ever looked at it. An expired licence is expired; a PHI
shorter than its own REI is impossible; neither becomes `Pending` because the
judgement stage was unavailable. `Pending` means "nothing definite was found
and nothing has judged it", not "we did not look".

────────────────────────────────────────────────────────────────────────────
CORRECTED FIELDS ARE PROPOSALS. NOTHING HERE OVERWRITES AN EXTRACTION
────────────────────────────────────────────────────────────────────────────

`corrected_fields` comes back beside `extracted_fields` and never in place of
it. Every entry says which rule proposed it, so a screen can show "the phone
read S24-S37, the shape of an EPA number says 524-537" and let a person decide.
An OCR correction applied silently is an OCR error nobody can find afterwards,
and the whole point of storing `ocr_text` next to `extraction_json` is that the
two can be compared by somebody who does not trust either.

────────────────────────────────────────────────────────────────────────────
THE REFERENCE TABLES ARE SMALL ON PURPOSE
────────────────────────────────────────────────────────────────────────────

`LABEL_REI_HOURS` holds the restricted-entry intervals for the active
ingredients that actually turn up in Pacific Northwest tree fruit, and it holds
them as a RANGE rather than a number, because one active ingredient carries
different REIs at different formulations and crops. A disagreement with it is a
WARNING naming the expected range — never an error and never an automatic
correction. The label is the law; this table is a reader's memory of the label,
and a table that overruled a photograph of the actual document would be exactly
the shadow record `compliance_fields.py` argues against.
"""

from __future__ import annotations

import datetime
import difflib
import json
import re

from erpnext_mcp import extraction_config

# ── the vocabulary ──────────────────────────────────────────────────────────

#: The `document_type` Select, in the doctype's own order. A caller passing
#: anything else is refused by name rather than validated against a default,
#: because "we checked it as a Receipt" is the wrong answer for a licence.
DOCUMENT_TYPES = (
	"Pesticide Label",
	"Applicator License",
	"WPS Certificate",
	"Insurance Certificate",
	"I-9 Document",
	"Receipt",
	"Inspection Evidence",
	"Task Evidence",
	"Signature",
	"Training Certificate",
)

#: The `validation_status` Select. `Pending` is the default and the honest
#: answer when nothing has judged the document — see the module docstring.
STATUS_PENDING = "Pending"
STATUS_VALIDATED = "Validated"
STATUS_FLAGGED = "Flagged"
STATUS_REJECTED = "Rejected"
VALIDATION_STATUSES = (STATUS_PENDING, STATUS_VALIDATED, STATUS_FLAGGED, STATUS_REJECTED)

#: Issue severities. `error` means a rule was broken and the document cannot be
#: relied on; `warning` means something disagrees with an expectation and a
#: person should look; `info` says what did not get checked.
ERROR = "error"
WARNING = "warning"
INFO = "info"
SEVERITIES = (ERROR, WARNING, INFO)

#: What each severity costs the confidence score. An error is worth three
#: warnings because an error is a fact about the document and a warning is a
#: disagreement with an expectation, and expectations here are a small table.
_SEVERITY_COST = {ERROR: 0.35, WARNING: 0.12, INFO: 0.03}

#: The floor. A document with six errors and one with nine are both "do not use
#: this", and a score that ran to zero would suggest a precision the arithmetic
#: does not have.
_CONFIDENCE_FLOOR = 0.05

#: The signal words a US pesticide label may carry, worst first. `None` is a
#: real answer — Category IV products carry no signal word at all — which is why
#: it is in the Select rather than left blank.
SIGNAL_WORDS = ("Danger", "Warning", "Caution", "None")

# ── EPA registration numbers ────────────────────────────────────────────────

#: `COMPANY-PRODUCT`, optionally `-DISTRIBUTOR`: 40 CFR 152.132. Every segment
#: is digits. This is the single most useful check in the file, because an EPA
#: registration number is the join key between a photographed label and every
#: downstream residue, re-entry and maximum-residue-limit question, and it is
#: also the field OCR mangles most reliably — it is printed small, it is printed
#: in a block of other small numbers, and it is all digits in a font where
#: several digits have letter twins.
EPA_REG_PATTERN = re.compile(r"^\d{1,7}-\d{1,6}(-\d{1,7})?$")

#: The letter→digit substitutions worth proposing when a registration number
#: misses the pattern. Deliberately not the full confusable set: `B`→`8` and
#: `Z`→`2` are real OCR confusions but are rare enough at this font size that
#: proposing them would produce more wrong corrections than right ones. Every
#: entry here is a glyph pair that is genuinely ambiguous in the small
#: condensed sans a registration number is printed in.
_OCR_DIGIT_LOOKALIKES = {
	"O": "0",
	"o": "0",
	"D": "0",
	"Q": "0",
	"I": "1",
	"l": "1",
	"i": "1",
	"|": "1",
	"S": "5",
	"s": "5",
	"G": "6",
	"T": "7",
}

#: The dash characters OCR hands back where a hyphen was printed. An EM DASH in
#: a registration number is not a different number, it is the same number read
#: through a wider kerning pair.
_DASHES = "‐‑‒–—−"

# ── restricted-entry intervals, by active ingredient ────────────────────────

#: Active ingredient (lowercased) → the plausible REI range in hours, as it
#: appears on US labels for tree fruit. A RANGE, not a number: one ingredient
#: carries different intervals at different formulations, crops and rates. See
#: the module docstring on why a disagreement here is a warning and never a
#: correction.
LABEL_REI_HOURS = {
	"abamectin": (12, 12),
	"acetamiprid": (12, 12),
	"azinphos-methyl": (14 * 24, 21 * 24),
	"bacillus thuringiensis": (4, 4),
	"captan": (24, 96),
	"carbaryl": (12, 24),
	"chlorantraniliprole": (4, 4),
	"chlorpyrifos": (24, 120),
	"copper hydroxide": (24, 48),
	"cyantraniliprole": (4, 4),
	"diazinon": (4 * 24, 5 * 24),
	"dodine": (48, 48),
	"esfenvalerate": (12, 24),
	"fenbuconazole": (12, 12),
	"horticultural oil": (4, 12),
	"imidacloprid": (12, 12),
	"kaolin": (4, 4),
	"lambda-cyhalothrin": (24, 24),
	"lime sulfur": (48, 48),
	"malathion": (12, 24),
	"mancozeb": (24, 24),
	"methoxyfenozide": (4, 4),
	"myclobutanil": (24, 24),
	"novaluron": (12, 12),
	"oxytetracycline": (12, 12),
	"permethrin": (12, 12),
	"phosmet": (5 * 24, 14 * 24),
	"potassium bicarbonate": (4, 4),
	"pyraclostrobin": (12, 12),
	"spinetoram": (4, 4),
	"spinosad": (4, 4),
	"streptomycin": (12, 12),
	"sulfur": (24, 24),
	"thiophanate-methyl": (12, 12),
	"trifloxystrobin": (12, 12),
	"zeta-cypermethrin": (12, 12),
}

#: The outer bounds a restricted-entry interval can take at all. Below the
#: first is not an interval; above the second is a fumigant's re-entry period
#: and not a tree-fruit label, so it is worth a person's eyes either way.
REI_MIN_HOURS = 1
REI_TYPICAL_MAX_HOURS = 21 * 24
REI_ABSOLUTE_MAX_HOURS = 60 * 24

#: The outer bounds for a pre-harvest interval, in days. A PHI of zero IS a
#: real answer — several labels permit harvest the same day — so zero is
#: allowed and only a negative one is refused.
PHI_MIN_DAYS = 0
PHI_TYPICAL_MAX_DAYS = 120
PHI_ABSOLUTE_MAX_DAYS = 365

#: `2 pt/acre`, `1.5 lb/A`, `32 fl oz per acre`. Loose on purpose: the point is
#: to catch an extraction that put a sentence in the rate field, not to parse
#: every rate a label can express.
APPLICATION_RATE_PATTERN = re.compile(
	r"^\s*\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?\s*)?[A-Za-z][A-Za-z .]{0,14}\s*(?:/|per\s)\s*[A-Za-z0-9]",
	re.IGNORECASE,
)

# ── how long an answer stays true ───────────────────────────────────────────

#: `document_type` → how many days a validation of it stays current, when the
#: document does not carry an expiry of its own. `None` means a validation of
#: this kind never goes stale: a receipt photographed in March is the same
#: receipt in December, and putting it on a revalidation list would bury the
#: documents that DO expire under the ones that cannot.
#:
#: A document carrying its own `expiration_date` uses that instead — see
#: `revalidation_due`. A licence is not due for revalidation on an anniversary
#: of when somebody scanned it; it is due when it expires.
REVALIDATION_DAYS = {
	"Pesticide Label": 365,
	"Applicator License": 365,
	"WPS Certificate": 365,
	"Insurance Certificate": 365,
	"I-9 Document": 365,
	"Training Certificate": 365,
	"Receipt": None,
	"Inspection Evidence": None,
	"Task Evidence": None,
	"Signature": None,
}

#: How close a scanned name has to be to the record it claims to belong to.
#: Above the first number is a match; between them is a warning naming both
#: spellings; below the second is an error. Tuned for the failure that actually
#: happens — an OCR'd surname losing an accent or a hyphen, or a licence
#: carrying a middle name the employee record does not — rather than for
#: catching a deliberate substitution, which no ratio catches.
NAME_MATCH_OK = 0.85
NAME_MATCH_DOUBTFUL = 0.62


# ── issues ──────────────────────────────────────────────────────────────────


def issue(code: str, severity: str, field: str, message: str) -> dict:
	"""One finding. `code` is stable and machine-readable, `message` is written
	for the person holding the phone.

	`field` is the extracted field the finding is about, or "" for a finding
	about the document as a whole. A screen highlights the field it names, so a
	code that named the wrong one would point somebody at a value that is fine.
	"""
	return {"code": code, "severity": severity, "field": field, "message": message}


def correction(value, rule: str, was) -> dict:
	"""One proposed correction, and the rule that proposed it.

	NEVER APPLIED HERE — see the module docstring. `was` is carried so a screen
	can show both readings without going back to `extraction_json`, and so a
	correction that turns out to be wrong can be traced to the rule that made
	it rather than to "the validator".
	"""
	return {"value": value, "rule": rule, "was": was}


# ── small conversions, each of which answers "not sent" differently ─────────


def _text(value) -> str:
	return str(value or "").strip()


def _as_int(value):
	"""`value` as an int, or `None` if it is not one. Never raises: an
	extraction is a machine's reading of a photograph and every field in it can
	be nonsense, which is a finding rather than a crash."""
	if value is None or isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value
	try:
		return int(str(value).strip())
	except (TypeError, ValueError):
		try:
			return int(float(str(value).strip()))
		except (TypeError, ValueError):
			return None


def _as_float(value):
	if value is None or isinstance(value, bool):
		return None
	try:
		return float(str(value).strip())
	except (TypeError, ValueError):
		return None


def _as_date(value):
	"""`value` as a `datetime.date`, or `None`. Accepts what a phone sends —
	`2026-08-14`, `2026-08-14 09:00:00`, and an ISO string with a `T`."""
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	text = _text(value)
	if not text:
		return None
	head = text.replace("T", " ").split(" ")[0]
	try:
		return datetime.date.fromisoformat(head)
	except ValueError:
		return None


def _today(as_of=None) -> datetime.date:
	"""The date every "is this in the past" check is made against.

	Taken as an argument rather than read from the clock so a test asserts a
	fixed answer and a caller replaying a document can ask what the answer WAS.
	"""
	return _as_date(as_of) or datetime.date.today()


def _normalised(text: str) -> str:
	"""Lowercase, with runs of non-alphanumerics collapsed to single spaces.

	What both sides of a "does this appear in the OCR text" check go through,
	so a hyphen OCR read as a space, or a line break where the label had none,
	does not become a finding on its own.
	"""
	return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _appears_in(needle: str, haystack: str) -> bool:
	"""Is `needle` in `haystack`, both normalised? False for an empty needle —
	an absent value is a different finding from an unsupported one, and the
	callers here have already made it."""
	needle_n = _normalised(needle)
	if not needle_n:
		return False
	return needle_n in _normalised(haystack)


def _digits_appear_in(value, haystack: str) -> bool:
	"""Does a number the extraction claims appear in the OCR text at all?

	Bare digits rather than the formatted value: a label printing `24 hours`
	and an extraction holding `24` are the same reading, and one printing
	`4 days` for a `96`-hour interval is a conversion the extraction did, which
	is why a miss here is a warning rather than an error.
	"""
	number = _text(value)
	if not number:
		return False
	return re.search(rf"(?<!\d){re.escape(number)}(?!\d)", str(haystack or "")) is not None


def name_similarity(left: str, right: str) -> float:
	"""How alike two people's names are, 0–1, order-insensitive.

	The tokens are sorted before comparing, so `Ana Ruiz Delgado` and
	`Delgado, Ana Ruiz` are the same name — which they are, and which a
	straight ratio scores at 0.55 and would flag on every licence that prints
	surname first.
	"""
	left_tokens = sorted(_normalised(left).split())
	right_tokens = sorted(_normalised(right).split())
	if not left_tokens or not right_tokens:
		return 0.0
	return difflib.SequenceMatcher(None, " ".join(left_tokens), " ".join(right_tokens)).ratio()


# ── EPA registration numbers ────────────────────────────────────────────────


def normalise_epa_number(raw: str) -> str:
	"""An EPA registration number with its whitespace and fancy dashes removed.

	Does NOT substitute digits — that is `repair_epa_number`, and the two are
	separate because normalising is lossless and repairing is a guess.
	"""
	text = _text(raw)
	for dash in _DASHES:
		text = text.replace(dash, "-")
	return re.sub(r"\s+", "", text)


def repair_epa_number(raw: str) -> str:
	"""The registration number an OCR misread most likely was, or "".

	Returns "" unless the substitution produces something that MATCHES the
	pattern — a repair that is still malformed is not a repair, and proposing
	one would replace a value a person can see is wrong with one they cannot.
	"""
	normalised = normalise_epa_number(raw)
	if not normalised or EPA_REG_PATTERN.match(normalised):
		return ""
	repaired = "".join(_OCR_DIGIT_LOOKALIKES.get(char, char) for char in normalised)
	return repaired if EPA_REG_PATTERN.match(repaired) else ""


# ── the per-type checkers ───────────────────────────────────────────────────
#
# Each takes the extraction, the OCR text and the context, and appends to
# `issues` and `corrections`. They share a shape rather than a base class
# because what they have in common is two output lists and nothing else —
# a receipt and a pesticide label do not have a common superclass in the world
# either.


#: v0.201.0. The two kinds of registered pesticide this app holds. A CROP
#: product is applied to a crop, and its label is where the REI, the PHI, the
#: crop and the rate per acre live. A NON-CROP product — a rodenticide, a bait
#: station, a structural insecticide — is used in and around buildings and
#: burrows, and has none of them. Both carry an EPA number, a signal word,
#: active ingredients, PPE and a storage and disposal statement.
SCOPE_CROP = "Crop"
SCOPE_NON_CROP = "Non-crop"
PESTICIDE_SCOPES = (SCOPE_CROP, SCOPE_NON_CROP)

_NON_CROP_WORDS = re.compile(
	r"\b(rats?|mice|mouse|rodents?|rodenticide|voles?|gophers?|bait stations?|burrows?|"
	r"in and around (?:homes|buildings)|structural|place pacs?)\b",
	re.IGNORECASE,
)
_CROP_WORDS = re.compile(
	r"\b(agricultural use requirements|restricted[- ]entry interval|pre-?harvest|per acre|/\s*a(?:cre)?\b|"
	r"orchards?|apples?|pears?|cherr(?:y|ies)|grapes?|crops?)",
	re.IGNORECASE,
)


_WORD_NUMBERS = {
	"one": 1,
	"two": 2,
	"three": 3,
	"four": 4,
	"five": 5,
	"six": 6,
	"seven": 7,
	"ten": 10,
	"fourteen": 14,
	"fifteen": 15,
}


def _count(token: str) -> int:
	token = token.strip().lower()
	return int(token) if token.isdigit() else _WORD_NUMBERS.get(token, 0)


def bait_label_facts(ocr_text: str) -> dict:
	"""v0.203.0. The five label facts a rodent bait placement is checked against.

	docs/design/rodent_bait_program.md §10. Read off the words a rodenticide label
	uses, and ONLY what the label says: a fact the text does not state is absent
	from the answer, never a guessed "allowed". The phone may send the same keys
	in `extracted_fields`; those win.
	"""
	text = " ".join(str(ocr_text or "").split())
	low = text.lower()
	out: dict = {}
	if re.search(r"tamper[\s-]*resistant\s+(?:bait\s+)?stations?", low) and re.search(
		r"(must|shall|required|only|children|pets|nontarget|non-target)", low
	):
		out["tamper_resistant_station_required"] = 1
	distances = [
		float(match.group(1))
		for match in re.finditer(
			r"within\s+(\d+(?:\.\d+)?)\s*(?:-\s*)?(?:feet|foot|ft)\b[^.]{0,60}?(?:building|structure)", low
		)
	]
	if distances:
		out["max_distance_from_structure_ft"] = min(distances)
	if re.search(r"(do not|don't|never)\s+(?:place|put|use|apply)[^.]{0,60}?burrows?", low) or re.search(
		r"burrow\s+baiting\s+(?:is\s+)?(?:prohibited|not\s+permitted|not\s+allowed)", low
	):
		out["burrow_baiting_allowed"] = "No"
	elif re.search(r"(?:place|apply|insert)[^.]{0,40}?(?:in|into)\s+(?:active\s+)?burrows?", low):
		out["burrow_baiting_allowed"] = "Yes"
	days = []
	for match in re.finditer(
		r"(?:at least|minimum of|for)\s+(\d+|one|two|three|four|five|six|seven|ten|fourteen|fifteen)\s*(days?|weeks?)",
		low,
	):
		around = low[max(0, match.start() - 80) : match.end() + 40]
		if "bait" not in around:
			continue
		number = _count(match.group(1))
		if number:
			days.append(number * (7 if match.group(2).startswith("week") else 1))
	if days:
		out["min_bait_days"] = min(days)
	if re.search(
		r"(outdoor use only|exterior use only|for outdoor use only|not for (?:indoor|interior) use)", low
	):
		out["interior_use_allowed"] = "No"
	elif re.search(r"(indoor|inside|in and around|interior)[^.]{0,30}?(use|building|structure|home)", low):
		out["interior_use_allowed"] = "Yes"
	return out


def pesticide_scope(fields: dict, ocr_text: str) -> str:
	"""`Crop` or `Non-crop`: what the extraction says, else what the label's words say.

	PROWLER (a bromethalin bait, "KILLS RATS, MICE & MEADOW VOLES", "Tamper-
	resistant bait stations") scored 0.05 because it has no REI, no PHI and no
	crop — three errors each — which is every rodenticide on the market. A label
	whose words are about rodents and buildings and none about crops is a
	non-crop product; anything else is judged as a crop label, which is the
	stricter reading and the one that was always applied.
	"""
	stated = _text((fields or {}).get("pesticide_use_scope"))
	for scope in PESTICIDE_SCOPES:
		if stated.lower().replace(" ", "-") == scope.lower():
			return scope
	text = str(ocr_text or "")
	if _NON_CROP_WORDS.search(text) and not _CROP_WORDS.search(text):
		return SCOPE_NON_CROP
	return SCOPE_CROP


def _check_pesticide_label(
	fields: dict, ocr_text: str, context: dict, issues: list, corrections: dict
) -> None:
	"""FIFRA label fields: the registration number, the two intervals, and
	whether the two intervals can both be true at once.

	v0.201.0. The intervals, the crop and the per-acre rate are asked of a CROP
	product only — see `pesticide_scope`. What EPA itself holds for the
	registration, when the caller looked it up, is checked against last."""
	_check_epa_number(fields, ocr_text, issues, corrections)
	_check_signal_word(fields, ocr_text, issues)
	if pesticide_scope(fields, ocr_text) == SCOPE_CROP:
		rei = _check_rei(fields, ocr_text, issues)
		phi = _check_phi(fields, ocr_text, issues)
		_check_rei_against_phi(rei, phi, issues)
		_check_rei_against_ingredients(fields, rei, issues)
		_check_application_rate(fields, issues)
	_check_active_ingredients(fields, issues)
	_check_ppe(fields, issues)
	_check_against_epa_record(fields, context.get("epa_record"), issues)


def _check_against_epa_record(fields: dict, record, issues: list) -> None:
	"""What the phone read, against what EPA registered. WARNINGS, never errors.

	A distributor's label (PROWLER, 12455-97-3240) is printed under its own name
	on the base registration (12455-97, F-TRAC PLACE PACS), so a different
	product name is expected and not checked. A different signal word or active
	ingredient is not expected: one of the two is misread, and a person should
	look.
	"""
	if not isinstance(record, dict) or not record:
		return
	registered = _text(record.get("signal_word"))
	read = _text(fields.get("signal_word"))
	if registered and read and registered.lower() != read.lower():
		issues.append(
			issue(
				"signal_word_differs_from_epa",
				WARNING,
				"signal_word",
				f"The label reads {read!r}; EPA registers {record.get('registration') or 'it'} as "
				f"{registered!r}. Check the tub.",
			)
		)
	registered_names = {
		_text(entry.get("name")).lower()
		for entry in record.get("active_ingredients") or []
		if entry.get("name")
	}
	read_names = {
		_text(entry.get("name")).lower()
		for entry in _ingredient_list(fields.get("active_ingredients"))
		if isinstance(entry, dict) and entry.get("name")
	}
	if registered_names and read_names and not (registered_names & read_names):
		issues.append(
			issue(
				"active_ingredients_differ_from_epa",
				WARNING,
				"active_ingredients",
				f"Read {', '.join(sorted(read_names))}; EPA registers "
				f"{', '.join(sorted(registered_names))}. One of the two is wrong.",
			)
		)


def _ingredient_list(raw) -> list:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			return []
	return raw if isinstance(raw, list) else []


def _check_epa_number(fields: dict, ocr_text: str, issues: list, corrections: dict) -> None:
	raw = _text(fields.get("epa_registration_number"))
	if not raw:
		issues.append(
			issue(
				"epa_registration_number_missing",
				ERROR,
				"epa_registration_number",
				"No EPA registration number was extracted. It is the number every residue, "
				"re-entry and maximum-residue-limit question is traced through, and a label "
				"record without it cannot be checked against a crop or a buyer's tolerance.",
			)
		)
		return

	normalised = normalise_epa_number(raw)
	if not EPA_REG_PATTERN.match(normalised):
		repaired = repair_epa_number(raw)
		if repaired:
			corrections["epa_registration_number"] = correction(repaired, "epa_reg_ocr_lookalikes", raw)
			issues.append(
				issue(
					"epa_registration_number_repairable",
					WARNING,
					"epa_registration_number",
					f"{raw!r} is not the shape of an EPA registration number (digits, one or two "
					f"hyphens — 40 CFR 152.132). Reading the letter-shaped characters as the "
					f"digits they resemble gives {repaired!r}, which is. Confirm it against the "
					f"label before it is used.",
				)
			)
		else:
			issues.append(
				issue(
					"epa_registration_number_malformed",
					ERROR,
					"epa_registration_number",
					f"{raw!r} is not the shape of an EPA registration number: 40 CFR 152.132 is "
					f"digits, a hyphen, digits, and optionally a hyphen and a distributor's "
					f"digits — 524-537 or 524-537-1381. No substitution of the usual OCR "
					f"look-alikes produces a well-formed number either.",
				)
			)
		return

	if normalised != raw:
		corrections["epa_registration_number"] = correction(normalised, "epa_reg_whitespace_and_dashes", raw)

	if ocr_text and not _appears_in(normalised.replace("-", " "), ocr_text):
		issues.append(
			issue(
				"epa_registration_number_not_in_ocr",
				WARNING,
				"epa_registration_number",
				f"{normalised} is well-formed but does not appear in the OCR text this "
				f"extraction came from. Either the number was read off a part of the label the "
				f"OCR did not capture, or it did not come off this photograph at all.",
			)
		)


def _check_signal_word(fields: dict, ocr_text: str, issues: list) -> None:
	word = _text(fields.get("signal_word"))
	if not word:
		issues.append(
			issue(
				"signal_word_missing",
				WARNING,
				"signal_word",
				"No signal word was extracted. Every registered pesticide label carries one or "
				"is Category IV and carries none — 'None' is the answer for the second case, and "
				"an empty field does not distinguish them.",
			)
		)
		return
	if word not in SIGNAL_WORDS:
		issues.append(
			issue(
				"signal_word_unrecognised",
				ERROR,
				"signal_word",
				f"{word!r} is not a signal word. A US label carries exactly one of "
				f"{', '.join(SIGNAL_WORDS)}, and the word decides the PPE the applicator wears.",
			)
		)
		return
	if word != "None" and ocr_text and not _appears_in(word, ocr_text):
		issues.append(
			issue(
				"signal_word_not_in_ocr",
				WARNING,
				"signal_word",
				f"The extraction says {word}, but the word does not appear in the OCR text. The "
				f"signal word is the largest print on a label, so an OCR pass that missed it "
				f"probably did not read the panel this field claims to come from.",
			)
		)


def _check_rei(fields: dict, ocr_text: str, issues: list):
	"""The restricted-entry interval, in hours. Returns it, or `None`."""
	raw = fields.get("rei_hours")
	if raw in (None, ""):
		issues.append(
			issue(
				"rei_hours_missing",
				ERROR,
				"rei_hours",
				"No restricted-entry interval was extracted. It is the crew-scheduling number — "
				"without it nobody can say when the block may be entered, and the label record "
				"cannot answer the one question asked the morning after a spray.",
			)
		)
		return None

	hours = _as_int(raw)
	if hours is None:
		issues.append(
			issue(
				"rei_hours_not_a_number",
				ERROR,
				"rei_hours",
				f"{raw!r} is not a number of hours. A restricted-entry interval is an integer "
				f"count of hours; a label printing '4 days' wants 96 here.",
			)
		)
		return None

	if hours < REI_MIN_HOURS:
		issues.append(
			issue(
				"rei_hours_out_of_range",
				ERROR,
				"rei_hours",
				f"An REI of {hours} hours is not an interval. The shortest interval on a US "
				f"label is 4 hours; nothing registered permits immediate entry.",
			)
		)
	elif hours > REI_ABSOLUTE_MAX_HOURS:
		issues.append(
			issue(
				"rei_hours_out_of_range",
				ERROR,
				"rei_hours",
				f"An REI of {hours} hours is {hours / 24:.0f} days, longer than any registered "
				f"tree-fruit interval. This is almost certainly a unit error — a label printing "
				f"the interval in days read as though it were hours.",
			)
		)
	elif hours > REI_TYPICAL_MAX_HOURS:
		issues.append(
			issue(
				"rei_hours_unusually_long",
				WARNING,
				"rei_hours",
				f"An REI of {hours} hours is {hours / 24:.0f} days. That is longer than every "
				f"common tree-fruit interval and worth confirming against the label — it is the "
				f"shape a hours-for-days unit error takes when the number is still plausible.",
			)
		)

	if ocr_text and not _digits_appear_in(hours, ocr_text):
		issues.append(
			issue(
				"rei_hours_not_in_ocr",
				WARNING,
				"rei_hours",
				f"{hours} does not appear anywhere in the OCR text. It may be a conversion the "
				f"extraction did — a label printing '4 days' for 96 hours — or a number that "
				f"came from somewhere other than this photograph.",
			)
		)
	return hours


def _check_phi(fields: dict, ocr_text: str, issues: list):
	"""The pre-harvest interval, in days, and the crop it applies to. Returns
	the interval, or `None`."""
	crop = _text(fields.get("phi_crop"))
	raw = fields.get("phi_days")

	if raw in (None, ""):
		issues.append(
			issue(
				"phi_days_missing",
				ERROR,
				"phi_days",
				"No pre-harvest interval was extracted. The pick date for every block this "
				"product goes on is planned off it weeks ahead, and a block picked inside its "
				"PHI is a residue violation on a shipped load.",
			)
		)
		days = None
	else:
		days = _as_int(raw)
		if days is None:
			issues.append(
				issue(
					"phi_days_not_a_number",
					ERROR,
					"phi_days",
					f"{raw!r} is not a number of days. A pre-harvest interval is an integer count "
					f"of days, and zero is a real answer — several labels permit same-day harvest.",
				)
			)
		elif days < PHI_MIN_DAYS:
			issues.append(
				issue(
					"phi_days_out_of_range",
					ERROR,
					"phi_days",
					f"A PHI of {days} days is not an interval.",
				)
			)
		elif days > PHI_ABSOLUTE_MAX_DAYS:
			issues.append(
				issue(
					"phi_days_out_of_range",
					ERROR,
					"phi_days",
					f"A PHI of {days} days is longer than a growing season, so no crop this "
					f"product is registered for could ever be harvested after an application.",
				)
			)
		elif days > PHI_TYPICAL_MAX_DAYS:
			issues.append(
				issue(
					"phi_days_unusually_long",
					WARNING,
					"phi_days",
					f"A PHI of {days} days is longer than every common tree-fruit interval. "
					f"Confirm it against the label — this is what an interval printed in hours "
					f"and read as days looks like.",
				)
			)
		if days is not None and ocr_text and not _digits_appear_in(days, ocr_text):
			issues.append(
				issue(
					"phi_days_not_in_ocr",
					WARNING,
					"phi_days",
					f"{days} does not appear anywhere in the OCR text this extraction came from.",
				)
			)

	if not crop:
		issues.append(
			issue(
				"phi_crop_missing",
				ERROR,
				"phi_crop",
				"The pre-harvest interval names no crop. One label carries a different PHI for "
				"cherries, apples and pears, so an interval with no crop beside it cannot be "
				"applied to a block — it is a number nobody can use.",
			)
		)
	elif ocr_text and not _appears_in(crop, ocr_text):
		issues.append(
			issue(
				"phi_crop_not_in_ocr",
				WARNING,
				"phi_crop",
				f"The PHI is recorded against {crop!r}, which does not appear in the OCR text. "
				f"If the crop was assumed rather than read, the interval beside it may belong to "
				f"a different one on the same label.",
			)
		)
	return days


def _check_rei_against_phi(rei_hours, phi_days, issues: list) -> None:
	"""The cross-check that needs both fields, and the reason they are checked
	together rather than one after the other.

	You cannot harvest a block you may not walk into. A PHI shorter than the
	REI is not a strict rule of the label — a few labels do carry one, where
	harvest is mechanical and nobody enters on foot — so this is an error only
	when the gap is wide enough that no reading reconciles it, and a warning
	when the two merely sit oddly together.
	"""
	if rei_hours is None or phi_days is None:
		return
	if phi_days * 24 >= rei_hours:
		return
	if phi_days == 0:
		issues.append(
			issue(
				"phi_shorter_than_rei",
				ERROR,
				"phi_days",
				f"The PHI is 0 days and the REI is {rei_hours} hours: the label would permit "
				f"harvesting a block on a day nobody may enter it on foot. One of the two "
				f"intervals was read off the wrong line.",
			)
		)
		return
	issues.append(
		issue(
			"phi_shorter_than_rei",
			WARNING,
			"phi_days",
			f"The PHI is {phi_days} day(s) — {phi_days * 24} hours — and the REI is {rei_hours} "
			f"hours, so the block becomes harvestable before it may be entered. That happens on "
			f"a mechanically harvested label and nowhere else; on tree fruit it usually means "
			f"the two intervals were swapped.",
		)
	)


def _check_rei_against_ingredients(fields: dict, rei_hours, issues: list) -> None:
	"""Does the interval match what a label for this active ingredient carries?

	The spec question this answers, and the one place a reference table is
	consulted at all. A WARNING naming the expected range and never a
	correction — see the module docstring on why the photograph outranks the
	table.
	"""
	if rei_hours is None:
		return
	for name in _ingredient_names(fields):
		known = LABEL_REI_HOURS.get(name)
		if not known:
			continue
		low, high = known
		if low <= rei_hours <= high:
			return
		expected = f"{low}" if low == high else f"{low}–{high}"
		issues.append(
			issue(
				"rei_disagrees_with_active_ingredient",
				WARNING,
				"rei_hours",
				f"The extracted REI is {rei_hours} hours, but labels carrying {name} carry "
				f"{expected} hours. The label is the law and this table is only a reader's "
				f"memory of it, so confirm against the photograph — but a gap this size is "
				f"usually a misread digit or an interval taken from the wrong product panel.",
			)
		)
		return


def _ingredient_names(fields: dict) -> list:
	"""The active ingredient names in an extraction, lowercased.

	Tolerant of the three shapes a phone sends: the contract's list of
	`{name, concentration, unit}`, a bare list of strings, and a single
	comma-separated string. Shape complaints belong to
	`_check_active_ingredients`; this one just wants the names.
	"""
	raw = fields.get("active_ingredients")
	if raw in (None, ""):
		return []
	if isinstance(raw, str):
		return [part.strip().lower() for part in raw.split(",") if part.strip()]
	if not isinstance(raw, list):
		return []
	names = []
	for entry in raw:
		if isinstance(entry, dict):
			name = _text(entry.get("name")).lower()
		else:
			name = _text(entry).lower()
		if name:
			names.append(name)
	return names


def _check_active_ingredients(fields: dict, issues: list) -> None:
	raw = fields.get("active_ingredients")
	if raw in (None, "", [], {}):
		issues.append(
			issue(
				"active_ingredients_missing",
				ERROR,
				"active_ingredients",
				"No active ingredients were extracted. The ingredient statement is what ties "
				"this product to a resistance-management group, to a re-entry interval and to "
				"every residue tolerance downstream.",
			)
		)
		return
	if not isinstance(raw, list):
		issues.append(
			issue(
				"active_ingredients_wrong_shape",
				WARNING,
				"active_ingredients",
				"active_ingredients is not a list. The stored shape is "
				"[{name, concentration, unit}] — one entry per ingredient — so a plain string "
				"here loses the concentration the label printed beside each name.",
			)
		)
		return

	percent_total = 0.0
	for index, entry in enumerate(raw):
		if not isinstance(entry, dict):
			issues.append(
				issue(
					"active_ingredient_wrong_shape",
					WARNING,
					"active_ingredients",
					f"Ingredient {index + 1} is {entry!r} rather than "
					f"{{name, concentration, unit}}, so its concentration was not captured.",
				)
			)
			continue
		if not _text(entry.get("name")):
			issues.append(
				issue(
					"active_ingredient_unnamed",
					ERROR,
					"active_ingredients",
					f"Ingredient {index + 1} has a concentration but no name.",
				)
			)
		unit = _text(entry.get("unit"))
		concentration = _as_float(entry.get("concentration"))
		if concentration is None:
			continue
		if concentration <= 0:
			issues.append(
				issue(
					"active_ingredient_concentration_invalid",
					WARNING,
					"active_ingredients",
					f"Ingredient {index + 1} has a concentration of {concentration}, which no "
					f"ingredient statement carries.",
				)
			)
		elif unit in ("%", "percent", "% w/w", "% by weight"):
			percent_total += concentration

	if percent_total > 100.0:
		issues.append(
			issue(
				"active_ingredients_exceed_100_percent",
				ERROR,
				"active_ingredients",
				f"The extracted ingredients total {percent_total:.2f}% by weight. An ingredient "
				f"statement including the inert fraction totals exactly 100%, so a figure above "
				f"it means a concentration was read from the wrong column or a decimal point "
				f"was lost.",
			)
		)


def _check_application_rate(fields: dict, issues: list) -> None:
	rate = _text(fields.get("application_rate"))
	if not rate:
		issues.append(
			issue(
				"application_rate_missing",
				WARNING,
				"application_rate",
				"No application rate was extracted. A label carries several — per crop, per "
				"pest, per method — so an absent one is common on a partial scan and is worth "
				"noting rather than refusing.",
			)
		)
		return
	if not APPLICATION_RATE_PATTERN.match(rate):
		issues.append(
			issue(
				"application_rate_unparseable",
				WARNING,
				"application_rate",
				f"{rate!r} is not a rate in the amount-per-area shape the field holds — "
				f"'2 pt/acre', '1.5 lb/A', '32 fl oz per acre'. A sentence here usually means "
				f"the extraction captured the surrounding instruction rather than the figure.",
			)
		)


def _check_ppe(fields: dict, issues: list) -> None:
	ppe = _text(fields.get("ppe_requirements"))
	if ppe:
		return
	if _text(fields.get("signal_word")) == "Danger":
		issues.append(
			issue(
				"ppe_requirements_missing",
				ERROR,
				"ppe_requirements",
				"The label carries the DANGER signal word and no PPE was extracted. Every "
				"DANGER product specifies personal protective equipment, and it is the sentence "
				"the applicator reads before mixing.",
			)
		)
		return
	issues.append(
		issue(
			"ppe_requirements_missing",
			WARNING,
			"ppe_requirements",
			"No PPE requirements were extracted. The PPE statement is what the applicator and "
			"any early-entry worker wear, and it is on every registered label.",
		)
	)


# ── credentials: licences, certificates, work authorisation ────────────────


def _check_applicator_license(
	fields: dict, ocr_text: str, context: dict, issues: list, corrections: dict
) -> None:
	_check_expiry(fields, context, issues, "license", "applicator licence")
	_check_identifier_in_ocr(fields, ocr_text, issues, "license_number", "licence number")
	_check_name_against_record(fields, context, issues, "licensee_name")
	if not _text(fields.get("issuing_state")):
		issues.append(
			issue(
				"issuing_state_missing",
				WARNING,
				"issuing_state",
				"No issuing state was extracted. A pesticide applicator licence is a state "
				"credential — Oregon's under ORS 634 — and one from a neighbouring state does "
				"not authorise an application here.",
			)
		)
	if not _text(fields.get("categories")) and not fields.get("categories"):
		issues.append(
			issue(
				"license_categories_missing",
				WARNING,
				"categories",
				"No licence categories were extracted. The categories are what the licence "
				"actually authorises, and a licence held for the wrong category is the same as "
				"no licence for the application being made.",
			)
		)


def _check_wps_certificate(
	fields: dict, ocr_text: str, context: dict, issues: list, corrections: dict
) -> None:
	_check_completion_date(fields, context, issues, "WPS worker or handler training")
	_check_name_against_record(fields, context, issues, "trainee_name")
	if not _text(fields.get("trainer_name")):
		issues.append(
			issue(
				"trainer_name_missing",
				WARNING,
				"trainer_name",
				"No trainer was extracted. 40 CFR 170.501 requires the trainer to be qualified, "
				"and the record of who trained somebody is what an inspection asks for after it "
				"has asked whether they were trained at all.",
			)
		)


def _check_training_certificate(
	fields: dict, ocr_text: str, context: dict, issues: list, corrections: dict
) -> None:
	_check_completion_date(fields, context, issues, "training")
	_check_name_against_record(fields, context, issues, "trainee_name")
	if not _text(fields.get("training_title")) and not _text(fields.get("course_name")):
		issues.append(
			issue(
				"training_title_missing",
				WARNING,
				"training_title",
				"The certificate names no course. A training record that cannot say what the "
				"training was in does not answer any question an audit asks of it.",
			)
		)


def _check_insurance_certificate(
	fields: dict, ocr_text: str, context: dict, issues: list, corrections: dict
) -> None:
	_check_expiry(fields, context, issues, "policy", "insurance certificate")
	_check_identifier_in_ocr(fields, ocr_text, issues, "policy_number", "policy number")
	if not _text(fields.get("carrier")) and not _text(fields.get("insurer")):
		issues.append(
			issue(
				"carrier_missing",
				WARNING,
				"carrier",
				"No carrier was extracted. A certificate of insurance with no insurer on it "
				"cannot be verified with anybody.",
			)
		)
	amount = _as_float(fields.get("coverage_amount"))
	if fields.get("coverage_amount") in (None, ""):
		issues.append(
			issue(
				"coverage_amount_missing",
				WARNING,
				"coverage_amount",
				"No coverage amount was extracted. The limit is the whole content of the "
				"certificate — a buyer or a landlord asking for one is asking for the number.",
			)
		)
	elif amount is None or amount <= 0:
		issues.append(
			issue(
				"coverage_amount_invalid",
				ERROR,
				"coverage_amount",
				f"{fields.get('coverage_amount')!r} is not a coverage limit.",
			)
		)


def _check_i9_document(fields: dict, ocr_text: str, context: dict, issues: list, corrections: dict) -> None:
	"""Work authorisation, and the one place an expiry is not merely a renewal.

	An expired List A or List C document is not paperwork out of date: it is a
	person who may not lawfully be put on a crew tomorrow. So the message says
	that rather than saying 'expired', and the severity is an error on a
	document whose expiry the extraction actually read.
	"""
	_check_name_against_record(fields, context, issues, "document_holder_name")
	if not _text(fields.get("document_title")):
		issues.append(
			issue(
				"document_title_missing",
				ERROR,
				"document_title",
				"No document title was extracted. Form I-9 records the TITLE of what was "
				"presented — 'US Passport', 'Permanent Resident Card' — and a Section 2 entry "
				"without one cannot be completed.",
			)
		)
	if not _text(fields.get("issuing_authority")):
		issues.append(
			issue(
				"issuing_authority_missing",
				WARNING,
				"issuing_authority",
				"No issuing authority was extracted. Form I-9 Section 2 asks for it beside the "
				"document title.",
			)
		)
	_check_identifier_in_ocr(fields, ocr_text, issues, "document_number", "document number")

	expiry = _as_date(fields.get("expiration_date"))
	today = _today(context.get("as_of"))
	if fields.get("expiration_date") in (None, ""):
		# Genuinely common and genuinely fine: a US passport card has one, a
		# birth certificate does not, and List B/C documents vary. So this is
		# information, not a finding against the document.
		issues.append(
			issue(
				"expiration_date_absent",
				INFO,
				"expiration_date",
				"No expiry was extracted. Several acceptable I-9 documents carry none, so this "
				"is only a problem if the document presented did.",
			)
		)
		return
	if expiry is None:
		issues.append(
			issue(
				"expiration_date_unreadable",
				ERROR,
				"expiration_date",
				f"{fields.get('expiration_date')!r} is not a date.",
			)
		)
		return
	if expiry < today:
		issues.append(
			issue(
				"work_authorization_expired",
				ERROR,
				"expiration_date",
				f"The document expired on {expiry.isoformat()}. This is not a filing problem: "
				f"re-verification is due, and until it is done this person cannot lawfully be "
				f"put on a crew.",
			)
		)
	elif (expiry - today).days <= 60:
		issues.append(
			issue(
				"work_authorization_expiring",
				WARNING,
				"expiration_date",
				f"The document expires on {expiry.isoformat()}, in {(expiry - today).days} days. "
				f"Re-verification has to be complete before it does, not after.",
			)
		)


# ── receipts and field evidence ────────────────────────────────────────────


def _check_receipt(fields: dict, ocr_text: str, context: dict, issues: list, corrections: dict) -> None:
	today = _today(context.get("as_of"))

	amount = _as_float(fields.get("amount")) if fields.get("amount") not in (None, "") else None
	if fields.get("amount") in (None, ""):
		issues.append(
			issue("amount_missing", ERROR, "amount", "No total was extracted, so nothing can be booked.")
		)
	elif amount is None:
		issues.append(
			issue("amount_not_a_number", ERROR, "amount", f"{fields.get('amount')!r} is not an amount.")
		)
	elif amount <= 0:
		issues.append(
			issue(
				"amount_not_positive",
				ERROR,
				"amount",
				f"A receipt total of {amount} is not a purchase. A refund belongs on a credit "
				f"note rather than as a negative receipt.",
			)
		)

	if not _text(fields.get("merchant")):
		issues.append(
			issue(
				"merchant_missing",
				WARNING,
				"merchant",
				"No merchant was extracted. The vendor is what decides the expense account and "
				"whether the spend needs a 1099 at year end.",
			)
		)

	receipt_date = _as_date(fields.get("receipt_date") or fields.get("date"))
	if fields.get("receipt_date") in (None, "") and fields.get("date") in (None, ""):
		issues.append(
			issue(
				"receipt_date_missing",
				WARNING,
				"receipt_date",
				"No date was extracted, so the spend cannot be placed in a period.",
			)
		)
	elif receipt_date is None:
		issues.append(
			issue(
				"receipt_date_unreadable",
				WARNING,
				"receipt_date",
				f"{fields.get('receipt_date') or fields.get('date')!r} is not a date.",
			)
		)
	elif receipt_date > today:
		issues.append(
			issue(
				"receipt_date_in_future",
				ERROR,
				"receipt_date",
				f"The receipt is dated {receipt_date.isoformat()}, which has not happened yet.",
			)
		)

	_check_line_items_sum(fields, amount, issues)


def _check_line_items_sum(fields: dict, total, issues: list) -> None:
	"""Do the lines add up to the total the receipt claims?

	Tolerant by a cent for rounding, and SILENT when tax or a tip is present
	and unextracted — a fuel slip whose lines miss the state excise line is a
	partial extraction rather than a wrong one, so the finding says the gap
	rather than asserting the total is wrong.
	"""
	items = fields.get("items") or fields.get("line_items")
	if total is None or not isinstance(items, list) or not items:
		return
	subtotal = 0.0
	seen = False
	for entry in items:
		if not isinstance(entry, dict):
			continue
		value = _as_float(entry.get("amount"))
		if value is None:
			value = _as_float(entry.get("total"))
		if value is None:
			rate = _as_float(entry.get("rate"))
			quantity = _as_float(entry.get("qty")) or _as_float(entry.get("quantity"))
			value = rate * quantity if rate is not None and quantity is not None else None
		if value is None:
			continue
		seen = True
		subtotal += value
	if not seen:
		return
	extras = sum(
		figure
		for figure in (_as_float(fields.get("tax")), _as_float(fields.get("tip")))
		if figure is not None
	)
	gap = round(total - (subtotal + extras), 2)
	if abs(gap) <= 0.01:
		return
	issues.append(
		issue(
			"line_items_do_not_sum",
			WARNING,
			"items",
			f"The extracted lines total {subtotal + extras:.2f} against a receipt total of "
			f"{total:.2f} — a gap of {gap:.2f}. Either a line was not captured, or tax was "
			f"charged and not extracted.",
		)
	)


def _check_evidence(fields: dict, ocr_text: str, context: dict, issues: list, corrections: dict) -> None:
	"""Inspection and task evidence: when it was captured, and where.

	Deliberately thin. Evidence is a photograph of a thing that was done, and
	almost nothing about it is checkable from the extraction — what makes it
	evidence is the task or the inspection it hangs off, which the record's
	`source_doctype`/`source_name` carry and this function does not second-guess.
	"""
	today = _today(context.get("as_of"))
	captured = _as_date(fields.get("captured_at") or fields.get("captured_on"))
	if fields.get("captured_at") in (None, "") and fields.get("captured_on") in (None, ""):
		issues.append(
			issue(
				"captured_at_missing",
				WARNING,
				"captured_at",
				"The evidence carries no capture time. Evidence that cannot say when it was "
				"taken proves the state of something at no particular moment.",
			)
		)
	elif captured is None:
		issues.append(
			issue(
				"captured_at_unreadable",
				WARNING,
				"captured_at",
				f"{fields.get('captured_at') or fields.get('captured_on')!r} is not a date.",
			)
		)
	elif captured > today:
		issues.append(
			issue(
				"captured_at_in_future",
				ERROR,
				"captured_at",
				f"The evidence claims to have been captured on {captured.isoformat()}, which has "
				f"not happened yet — usually a device whose clock is wrong, which matters "
				f"because the timestamp is the whole value of the record.",
			)
		)


def _check_signature(fields: dict, ocr_text: str, context: dict, issues: list, corrections: dict) -> None:
	today = _today(context.get("as_of"))
	if not _text(fields.get("signer_name")):
		issues.append(
			issue(
				"signer_name_missing",
				ERROR,
				"signer_name",
				"The signature names nobody. An unattributable signature is not evidence that "
				"anybody agreed to anything.",
			)
		)
	else:
		_check_name_against_record(fields, context, issues, "signer_name")

	signed = _as_date(fields.get("signed_at"))
	if fields.get("signed_at") in (None, ""):
		issues.append(
			issue(
				"signed_at_missing",
				ERROR,
				"signed_at",
				"The signature carries no time. When somebody signed is half of what a signature proves.",
			)
		)
	elif signed is None:
		issues.append(
			issue("signed_at_unreadable", ERROR, "signed_at", f"{fields.get('signed_at')!r} is not a date.")
		)
	elif signed > today:
		issues.append(
			issue(
				"signed_at_in_future",
				ERROR,
				"signed_at",
				f"The signature is dated {signed.isoformat()}, which has not happened yet.",
			)
		)


# ── checks several document types share ────────────────────────────────────


def _check_expiry(fields: dict, context: dict, issues: list, noun: str, document: str) -> None:
	"""The expiry check every credential shares: present, readable, plausible,
	and not already in the past."""
	today = _today(context.get("as_of"))
	raw = fields.get("expiration_date")
	if raw in (None, ""):
		issues.append(
			issue(
				"expiration_date_missing",
				ERROR,
				"expiration_date",
				f"No expiry was extracted. A {document} with no expiry on file is one nobody can "
				f"be told to renew, which is how a lapse is discovered by an inspector rather "
				f"than by a calendar.",
			)
		)
		return

	expiry = _as_date(raw)
	if expiry is None:
		issues.append(
			issue(
				"expiration_date_unreadable",
				ERROR,
				"expiration_date",
				f"{raw!r} is not a date.",
			)
		)
		return

	issued = _as_date(fields.get("issue_date") or fields.get("effective_date"))
	if issued and expiry <= issued:
		issues.append(
			issue(
				"expiration_precedes_issue",
				ERROR,
				"expiration_date",
				f"The {noun} is recorded as expiring on {expiry.isoformat()}, on or before the "
				f"{issued.isoformat()} it was issued. The two dates were read the wrong way round.",
			)
		)
		return

	if expiry < today:
		issues.append(
			issue(
				f"{noun}_expired",
				ERROR,
				"expiration_date",
				f"The {noun} expired on {expiry.isoformat()}, {(today - expiry).days} days ago.",
			)
		)
		return

	if (expiry - today).days > 10 * 365:
		issues.append(
			issue(
				"expiration_date_implausible",
				WARNING,
				"expiration_date",
				f"The {noun} is recorded as running to {expiry.isoformat()}, more than ten years "
				f"out. No credential of this kind is issued for that long, so the year was "
				f"probably misread.",
			)
		)
		return

	if (expiry - today).days <= 30:
		issues.append(
			issue(
				f"{noun}_expiring",
				WARNING,
				"expiration_date",
				f"The {noun} expires on {expiry.isoformat()}, in {(expiry - today).days} days.",
			)
		)


def _check_completion_date(fields: dict, context: dict, issues: list, what: str) -> None:
	"""A certificate says when the training happened, not when it expires."""
	today = _today(context.get("as_of"))
	raw = fields.get("completion_date") or fields.get("training_date")
	if raw in (None, ""):
		issues.append(
			issue(
				"completion_date_missing",
				ERROR,
				"completion_date",
				f"No completion date was extracted. The date is what says whether the {what} is "
				f"current, and a certificate without one cannot be put on a renewal calendar.",
			)
		)
		return
	completed = _as_date(raw)
	if completed is None:
		issues.append(
			issue("completion_date_unreadable", ERROR, "completion_date", f"{raw!r} is not a date.")
		)
		return
	if completed > today:
		issues.append(
			issue(
				"completion_date_in_future",
				ERROR,
				"completion_date",
				f"The certificate is dated {completed.isoformat()}, which has not happened yet.",
			)
		)
		return
	if (today - completed).days > 365:
		issues.append(
			issue(
				"training_out_of_date",
				WARNING,
				"completion_date",
				f"The {what} was completed on {completed.isoformat()}, "
				f"{(today - completed).days} days ago. WPS training is annual, and most "
				f"certificate programmes behind it are too.",
			)
		)


def _check_identifier_in_ocr(fields: dict, ocr_text: str, issues: list, field: str, noun: str) -> None:
	value = _text(fields.get(field))
	if not value:
		issues.append(
			issue(
				f"{field}_missing",
				ERROR,
				field,
				f"No {noun} was extracted. It is what the document is verified with at the "
				f"issuing authority, and a record without it can only be checked by finding the "
				f"paper again.",
			)
		)
		return
	if ocr_text and not _appears_in(value, ocr_text):
		issues.append(
			issue(
				f"{field}_not_in_ocr",
				WARNING,
				field,
				f"The {noun} {value!r} does not appear in the OCR text this extraction came from.",
			)
		)


def _check_name_against_record(fields: dict, context: dict, issues: list, field: str) -> None:
	"""Does the name on the document match the record it is being filed against?

	The spec's "does name match employee record" check, and the reason
	`validate_extraction` takes a `context` at all. Silent when the caller did
	not supply an expected name — a document filed against nothing has nothing
	to disagree with, and inventing a finding there would train people to
	ignore this code.
	"""
	expected = _text(context.get("expected_name") or context.get("employee_name"))
	found = _text(fields.get(field) or fields.get("name") or fields.get("full_name"))
	if not expected:
		return
	if not found:
		issues.append(
			issue(
				f"{field}_missing",
				WARNING,
				field,
				f"No name was extracted from the document, so it cannot be checked against "
				f"{expected!r} — the record it is being filed against.",
			)
		)
		return

	ratio = name_similarity(found, expected)
	if ratio >= NAME_MATCH_OK:
		return
	if ratio >= NAME_MATCH_DOUBTFUL:
		issues.append(
			issue(
				"name_partially_matches_record",
				WARNING,
				field,
				f"The document reads {found!r} and the record it is filed against is {expected!r}. "
				f"Close enough to be an OCR difference or a missing middle name, far enough "
				f"apart to be worth one person's eyes.",
			)
		)
		return
	issues.append(
		issue(
			"name_does_not_match_record",
			ERROR,
			field,
			f"The document reads {found!r} and the record it is being filed against is "
			f"{expected!r}. These are not the same name, so either the document belongs to "
			f"somebody else or it was filed against the wrong person.",
		)
	)


#: `document_type` → the checker that knows what that document has to contain.
#: Three types share `_check_evidence` because what makes a photograph evidence
#: is the task it hangs off rather than anything readable inside it.
_CHECKERS = {
	"Pesticide Label": _check_pesticide_label,
	"Applicator License": _check_applicator_license,
	"WPS Certificate": _check_wps_certificate,
	"Insurance Certificate": _check_insurance_certificate,
	"I-9 Document": _check_i9_document,
	"Receipt": _check_receipt,
	"Inspection Evidence": _check_evidence,
	"Task Evidence": _check_evidence,
	"Signature": _check_signature,
	"Training Certificate": _check_training_certificate,
}

#: The fields each document type is expected to carry, used for the coverage
#: half of the confidence score — how much of the document the extraction
#: actually got, as distinct from how much of what it got is wrong. A document
#: with two fields extracted and no errors is not a validated document; it is a
#: barely-read one, and the score has to say so.
EXPECTED_FIELDS = {
	"Pesticide Label": (
		"epa_registration_number",
		"signal_word",
		"rei_hours",
		"phi_days",
		"phi_crop",
		"active_ingredients",
		"application_rate",
		"ppe_requirements",
	),
	"Applicator License": (
		"license_number",
		"licensee_name",
		"expiration_date",
		"issuing_state",
		"categories",
	),
	"WPS Certificate": ("trainee_name", "completion_date", "trainer_name"),
	"Insurance Certificate": ("policy_number", "carrier", "expiration_date", "coverage_amount"),
	"I-9 Document": ("document_title", "issuing_authority", "document_number"),
	"Receipt": ("merchant", "amount", "receipt_date"),
	"Inspection Evidence": ("captured_at",),
	"Task Evidence": ("captured_at",),
	"Signature": ("signer_name", "signed_at"),
	"Training Certificate": ("trainee_name", "completion_date", "training_title"),
}


# ── the entry points ────────────────────────────────────────────────────────


def normalise_document_type(raw: str) -> str:
	"""The `document_type` Select value, matched case-insensitively, or "".

	Returns "" rather than raising: the caller has the vocabulary to put in the
	refusal and this module has no opinion about how a tool phrases one.
	"""
	wanted = _normalised(raw)
	if not wanted:
		return ""
	for known in DOCUMENT_TYPES:
		if _normalised(known) == wanted:
			return known
	return ""


#: v0.201.0. What a NON-CROP pesticide label carries — no intervals, no crop.
NON_CROP_EXPECTED_FIELDS = (
	"epa_registration_number",
	"signal_word",
	"active_ingredients",
	"ppe_requirements",
	"application_rate",
	"storage_disposal",
)


def extraction_coverage(document_type: str, fields: dict, ocr_text: str = "") -> float:
	"""How much of what this document type carries the extraction actually got,
	0–1. 1.0 for a type with no expected fields declared."""
	expected = EXPECTED_FIELDS.get(document_type, ())
	if document_type == "Pesticide Label" and pesticide_scope(fields, ocr_text) == SCOPE_NON_CROP:
		expected = NON_CROP_EXPECTED_FIELDS
	if not expected:
		return 1.0
	present = sum(1 for name in expected if (fields or {}).get(name) not in (None, "", [], {}))
	return present / len(expected)


def score_confidence(issues, coverage: float) -> float:
	"""What the deterministic pass believes, 0–1.

	TWO INDEPENDENT PENALTIES, MULTIPLIED RATHER THAN AVERAGED. Issues say how
	much of what was read is wrong; coverage says how much was read at all. A
	document that is 30% extracted and clean is not 65% trustworthy — averaging
	would say it was — it is a document nobody has really checked, and
	multiplying is what makes the score say that.

	Coverage is floored at 0.4 so an empty extraction still scores above the
	floor rather than at zero: 0.0 would claim the document is known to be
	wrong, and what is actually known is that almost nothing was read.
	"""
	penalty = sum(_SEVERITY_COST.get(entry.get("severity"), 0.0) for entry in issues or ())
	from_issues = max(_CONFIDENCE_FLOOR, 1.0 - penalty)
	return round(max(_CONFIDENCE_FLOOR, from_issues * max(0.4, min(1.0, coverage))), 4)


def revalidation_due(document_type: str, fields: dict, as_of=None) -> str:
	"""When this validation should be run again, as `YYYY-MM-DD`, or "".

	THE DOCUMENT'S OWN EXPIRY WINS. A licence is not due for revalidation on
	the anniversary of somebody scanning it; it is due when it expires, and a
	cadence that ignored the expiry would put a licence lapsing next month
	behind one scanned eleven months ago.

	"" for the types that never go stale — see `REVALIDATION_DAYS`.
	"""
	document_type = normalise_document_type(document_type) or document_type
	if document_type not in REVALIDATION_DAYS:
		return ""
	today = _today(as_of)

	expiry = _as_date((fields or {}).get("expiration_date"))
	cadence = REVALIDATION_DAYS.get(document_type)
	if expiry and expiry > today:
		if cadence is None:
			return expiry.isoformat()
		return min(expiry, today + datetime.timedelta(days=cadence)).isoformat()
	if cadence is None:
		return ""
	return (today + datetime.timedelta(days=cadence)).isoformat()


def validate_extraction(document_type: str, ocr_text: str, extracted_fields: dict, context=None) -> dict:
	"""The deterministic half, whole. Reads nothing, writes nothing, never raises.

	`context` carries what the checks need from outside the document —
	`expected_name` for the name match, `as_of` for every date comparison — and
	an absent one simply means those checks stay silent.

	Returns `status`, `confidence`, `issues`, `corrected_fields`, `reasoning`
	and the two figures the score is built from. `status` here is only ever
	`Flagged` (an error was found) or `Pending` (nothing definite, and nothing
	has judged it) — `Validated` and `Rejected` are judgements, and this
	function makes none. `merge_llm_assessment` is where they can arrive.
	"""
	document_type = normalise_document_type(document_type) or document_type
	fields = extracted_fields if isinstance(extracted_fields, dict) else {}
	ocr_text = str(ocr_text or "")
	context = context if isinstance(context, dict) else {}

	issues: list = []
	corrections: dict = {}

	checker = _CHECKERS.get(document_type)
	if checker is None:
		issues.append(
			issue(
				"document_type_unknown",
				WARNING,
				"document_type",
				f"{document_type!r} has no checks of its own, so only the shape of the "
				f"extraction was looked at. One of {', '.join(DOCUMENT_TYPES)} gets the "
				f"document-specific rules.",
			)
		)
	else:
		if not fields:
			issues.append(
				issue(
					"extraction_empty",
					ERROR,
					"",
					"The extraction carried no fields at all, so there was nothing to check. "
					"Either on-device extraction found nothing on this image, or the fields "
					"were not sent with it.",
				)
			)
		checker(fields, ocr_text, context, issues, corrections)

	if not ocr_text:
		issues.append(
			issue(
				"ocr_text_absent",
				INFO,
				"ocr_text",
				"No OCR text came with the extraction, so every check that compares a field "
				"against what was actually printed was skipped. The values were checked against "
				"each other and against the rules, and against nothing on the page.",
			)
		)

	coverage = extraction_coverage(document_type, fields, ocr_text)
	confidence = score_confidence(issues, coverage)
	errors = [entry for entry in issues if entry["severity"] == ERROR]

	scope = (
		{"pesticide_use_scope": pesticide_scope(fields, ocr_text)}
		if document_type == "Pesticide Label"
		else {}
	)
	return {
		**scope,
		"document_type": document_type,
		"status": STATUS_FLAGGED if errors else STATUS_PENDING,
		"confidence": confidence,
		"coverage": round(coverage, 4),
		"issues": issues,
		"corrected_fields": corrections,
		"error_count": len(errors),
		"warning_count": len([entry for entry in issues if entry["severity"] == WARNING]),
		"reasoning": _reasoning(document_type, issues, coverage, checker is not None),
		"revalidation_due": revalidation_due(document_type, fields, context.get("as_of")),
	}


def _reasoning(document_type: str, issues: list, coverage: float, had_checker: bool) -> str:
	"""One paragraph a person reads instead of counting the issue list."""
	errors = [entry for entry in issues if entry["severity"] == ERROR]
	warnings = [entry for entry in issues if entry["severity"] == WARNING]
	if not had_checker:
		return (
			f"No document-specific rules exist for {document_type!r}, so nothing about the "
			f"content was checked."
		)
	head = (
		f"{len(errors)} error(s) and {len(warnings)} warning(s) from the deterministic checks "
		f"for {document_type}, over an extraction carrying {coverage:.0%} of the fields this "
		f"document type is expected to have."
	)
	if errors:
		return f"{head} Flagged on: {'; '.join(entry['code'] for entry in errors[:6])}."
	if warnings:
		return (
			f"{head} Nothing definite was found. Worth a look: "
			f"{'; '.join(entry['code'] for entry in warnings[:6])}."
		)
	return f"{head} Every rule passed."


# ── the judgement half ──────────────────────────────────────────────────────


def validate_llm_assessment(raw) -> tuple:
	"""`(assessment, problems)` — the client's assessment, cleaned, and what was
	wrong with it.

	NEVER RAISES AND NEVER DISCARDS SILENTLY. An assessment that is the wrong
	shape becomes an `info` issue on the record saying so, because the failure
	worth catching here is a model whose judgement was quietly dropped and a
	document that therefore sat at `Pending` for a reason nobody could see.
	"""
	problems: list = []
	if raw in (None, "", {}):
		return None, problems
	if not isinstance(raw, dict):
		problems.append(
			issue(
				"llm_assessment_wrong_shape",
				INFO,
				"llm_assessment",
				f"The assessment was {type(raw).__name__} rather than an object shaped "
				f"{{status, issues, confidence, reasoning}}, so it was recorded as given and "
				f"not used to decide the status.",
			)
		)
		return None, problems

	status = normalise_status(raw.get("status"))
	if raw.get("status") not in (None, "") and not status:
		problems.append(
			issue(
				"llm_status_unrecognised",
				INFO,
				"llm_assessment",
				f"The assessment's status was {raw.get('status')!r}, which is not one of "
				f"{', '.join(VALIDATION_STATUSES)}. The deterministic status stands.",
			)
		)

	confidence = _as_float(raw.get("confidence"))
	if raw.get("confidence") not in (None, "") and (confidence is None or not 0.0 <= confidence <= 1.0):
		problems.append(
			issue(
				"llm_confidence_out_of_range",
				INFO,
				"llm_assessment",
				f"The assessment's confidence was {raw.get('confidence')!r} rather than a number "
				f"between 0 and 1, so it was left out of the combined score.",
			)
		)
		confidence = None

	llm_issues = []
	for entry in raw.get("issues") or ():
		if isinstance(entry, dict):
			llm_issues.append(
				issue(
					_text(entry.get("code")) or "llm_finding",
					entry.get("severity") if entry.get("severity") in SEVERITIES else WARNING,
					_text(entry.get("field")),
					_text(entry.get("message")),
				)
			)
		elif _text(entry):
			llm_issues.append(issue("llm_finding", WARNING, "", _text(entry)))

	return (
		{
			"status": status,
			"confidence": confidence,
			"issues": llm_issues,
			"reasoning": _text(raw.get("reasoning")),
		},
		problems,
	)


def normalise_status(raw: str) -> str:
	"""One of `VALIDATION_STATUSES`, matched case-insensitively, or ""."""
	wanted = _normalised(raw)
	if not wanted:
		return ""
	for known in VALIDATION_STATUSES:
		if _normalised(known) == wanted:
			return known
	return ""


#: v0.202.0. Models whose assessment is ADVISORY — kept as warnings, never the
#: deciding vote. Matched as a prefix of `llm_model`.
ADVISORY_MODEL_PREFIXES = ("apple-",)


def is_advisory_model(llm_model) -> bool:
	return _text(llm_model).lower().startswith(ADVISORY_MODEL_PREFIXES)


#: v0.202.0. How long an advisory finding's message may be. The on-device model
#: wrote paragraphs; a review screen shows a line.
ADVISORY_MESSAGE_LIMIT = 240


def _contradicted(entry: dict, deterministic: dict, config=None) -> str:
	"""Why a rule contradicts this advisory finding, or "" when none does. §8.3.

	v0.206.0: the list is DATA — the document type's `advisory_drop` in its
	Extraction Config (docs/design/config_flags_triage.md §1.2). The built-in
	Pesticide Label config reproduces the v0.202.0 rules exactly: an EPA finding
	the rules did not also raise, a crop-only finding on a Non-crop label, and
	"ingredients total 100%"."""
	if config is None:
		config, _version = _extraction_config(deterministic.get("document_type"))
	return extraction_config.drop_reason(entry, config, deterministic)


def _extraction_config(document_type):
	try:
		return extraction_config.active(_text(document_type))
	except Exception:
		return extraction_config.builtin(_text(document_type))


def merge_llm_assessment(deterministic: dict, assessment, llm_model: str = "") -> dict:
	"""The deterministic result and the client's judgement, resolved into one.

	  THE RULES, IN THE ORDER THEY BIND:

	1. A DETERMINISTIC ERROR OUTRANKS EVERYTHING. An expired licence is
	   expired whatever a model thinks of the photograph, so the merged
	   status stays `Flagged` — or becomes `Rejected` if the model went
	   further. A model cannot talk a rule out of a fact.
	2. `Rejected` OUTRANKS `Flagged` OUTRANKS `Pending` OUTRANKS `Validated`.
	   The worst reading wins, because the cost of looking at a document that
	   turned out to be fine is a minute and the cost of the other mistake is
	   an inspection finding.
	3. NO ASSESSMENT MEANS `Pending`, AND SAYS SO. An `info` issue names the
	   reason, so a document sitting at Pending can always be traced to
	   either "nothing judged it" or "something judged it and was unsure".

	  The merged confidence is the LOWER of the two rather than a mean: they are
	  two independent readings of the same document, and if either is unconvinced
	  the document is not convincing.
	"""
	merged = dict(deterministic)
	issues = list(deterministic.get("issues") or ())
	det_status = deterministic.get("status") or STATUS_PENDING
	det_errors = bool(deterministic.get("error_count"))

	if not assessment:
		issues.append(
			issue(
				"llm_validation_unavailable",
				INFO,
				"",
				"No LLM assessment came with this call, so the judgement half of validation did "
				"not run: everything below is the deterministic checks alone. The status is "
				"Pending unless a rule found something definite — which is a statement about "
				"what was checked, not about the document.",
			)
		)
		merged["issues"] = issues
		merged["llm_model"] = ""
		merged["llm_available"] = False
		merged["status"] = STATUS_FLAGGED if det_errors else STATUS_PENDING
		merged["reasoning"] = (
			f"{deterministic.get('reasoning', '')} No LLM assessment was supplied, so nothing "
			f"has judged the document's authenticity or plausibility."
		).strip()
		return merged

	llm_status = assessment.get("status") or ""
	llm_confidence = assessment.get("confidence")
	model_issues = list(assessment.get("issues") or ())
	if is_advisory_model(llm_model):
		# v0.202.0. A PHONE'S ON-DEVICE MODEL IS HEARD, NOT OBEYED. On OML its
		# assessment of PROWLER® said a well-formed 12455-97-3240 "lacks
		# hyphens", that ingredients totalling 100% were "implausible", and that
		# a mouse bait needs an REI — and flagged the record on its own. Its
		# findings are kept, as warnings, for a person to read; the status and
		# the confidence stay the rules'. An MCP client's assessment is not
		# affected: that is a model a person chose to run the judgement.
		#
		# v0.202.0 (contract §8.3). A finding a RULE CONTRADICTS is dropped, not
		# kept as a warning — a reviewer shown "lacks hyphens" beside a number
		# the rules accepted learns to ignore the column. The rest are cut to
		# `ADVISORY_MESSAGE_LIMIT` characters.
		config, config_version = _extraction_config(deterministic.get("document_type"))
		limit = extraction_config.message_limit(config) if config else ADVISORY_MESSAGE_LIMIT
		kept = []
		for entry in model_issues:
			if not isinstance(entry, dict) or _contradicted(entry, deterministic, config):
				continue
			message = _text(entry.get("message"))
			if len(message) > limit:
				message = message[: limit - 1].rstrip() + "…"
			kept.append(
				{
					**entry,
					"message": message,
					"severity": WARNING if entry.get("severity") == ERROR else entry.get("severity"),
				}
			)
		merged["advisory_dropped"] = len(model_issues) - len(kept)
		if config_version:
			merged.setdefault("config_version", config_version)
		model_issues = kept
		llm_status = ""
		llm_confidence = None
	issues.extend(model_issues)

	if det_errors:
		status = STATUS_REJECTED if llm_status == STATUS_REJECTED else STATUS_FLAGGED
	elif llm_status:
		status = llm_status
	else:
		status = det_status

	confidence = deterministic.get("confidence", 0.0)
	if llm_confidence is not None:
		confidence = round(min(confidence, llm_confidence), 4)

	merged["issues"] = issues
	merged["status"] = status
	merged["confidence"] = confidence
	merged["llm_model"] = _text(llm_model)
	merged["llm_available"] = True
	merged["error_count"] = len([entry for entry in issues if entry["severity"] == ERROR])
	merged["warning_count"] = len([entry for entry in issues if entry["severity"] == WARNING])
	merged["reasoning"] = " ".join(
		part
		for part in (
			deterministic.get("reasoning", ""),
			f"The assessment from {_text(llm_model) or 'an unnamed model'} read "
			f"{llm_status or 'no status'}"
			+ (f" at confidence {llm_confidence}" if llm_confidence is not None else "")
			+ ".",
			assessment.get("reasoning", ""),
		)
		if part
	).strip()
	return merged
