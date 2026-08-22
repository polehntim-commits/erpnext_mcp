# SPDX-License-Identifier: MIT
"""GS1 identifiers: the check digit, the three keys built on it, and who may issue one.

WHAT A GTIN IS AND WHY GETTING IT WRONG IS NOT A COSMETIC BUG. A GTIN is the
number a buyer's receiving scanner reads off a case, a GLN is the number that
says which packing house shipped it, and an SSCC is the number on the pallet
label that ties the two together in an FSMA §204 traceability record. All three
are GS1 keys: a company prefix the farm was ISSUED, a reference the farm
allocates, and a check digit computed over both. The check digit is what makes a
mis-keyed digit fail at the scanner instead of silently naming somebody else's
product.

THE PREFIX IS THE PART THAT CANNOT BE INVENTED, AND THIS MODULE REFUSES TO
INVENT IT. The farm_app this came from fell back to a literal `"DEV123"` when no
prefix was configured, which produced a well-formed number in an address space
GS1 issued to nobody — non-numeric, so it could not even be scanned, and printed
onto labels regardless. Worse than the malformed case is the plausible one: a
short numeric fallback lands inside a prefix range GS1 DID issue, to a real
company, and the farm ships cases claiming to be theirs. So `prefix_or_raise`
raises with the reason, and every generator here goes through it. A farm without
a GS1 licence gets an error telling it to get one, or uses the custom mode
below.

CUSTOM MODE IS FOR FARMS THAT DO NOT SELL INTO GS1 CHANNELS, and it is honest
about what it is. A direct-market orchard needs unique case identifiers and does
not need a $2,000/yr GS1 licence to have them. `standard="custom"` builds the
same fixed-length, check-digit-protected numbers from a prefix the farm chose,
and `describe()` reports `gs1_compliant: False` so nothing downstream can
present one to a buyer as a GS1 key. The number is internally sound and
externally meaningless, which is exactly the deal.

WHY THE ARITHMETIC IS HERE AND NOT IN A LIBRARY. It is nine lines, it is
specified in GS1 General Specifications §7.9, and it has not changed since 1973.
A dependency for it would be a supply-chain risk taken on to avoid writing a
loop.

WHERE THE PREFIX COMES FROM ON A REAL SITE. `configured_prefix()` reads
`ERPNext MCP Settings` if — and only if — that doctype carries a
`gs1_company_prefix` field. No release has added one yet, so on every site today
it answers `""` and callers must pass the prefix explicitly. That is deliberate:
adding the field is a settings-schema change with its own release, and a module
that read a field it had not shipped would fail on the bench rather than in the
suite. The lookup is written now so that release is a JSON edit and not a code
change.
"""

from __future__ import annotations

#: The GTIN lengths GS1 defines. GTIN-8 is issued by GS1 itself for very small
#: packages and cannot be constructed from a company prefix, so it validates
#: here and does not generate — a farm that has one was given it.
GTIN_LENGTHS = (12, 13, 14)

#: Every GS1 key length this module will validate a check digit on.
KEY_LENGTHS = (8, 12, 13, 14, 18)

#: A GLN is always 13 digits and an SSCC always 18. Named because the error
#: messages quote them and a number that disagrees with its own length is the
#: most common hand-entry failure.
GLN_LENGTH = 13
SSCC_LENGTH = 18

#: The two traceability standards a site can be on. `gs1` means the farm holds a
#: GS1 licence and its keys are globally unique; `custom` means the numbers are
#: the farm's own and mean nothing off the farm.
STANDARDS = ("gs1", "custom")

#: The settings field a later release will add, named once. See the module
#: docstring for why it is read defensively rather than assumed.
PREFIX_FIELD = "gs1_company_prefix"
CUSTOM_PREFIX_FIELD = "custom_identifier_prefix"


class GS1Error(ValueError):
	"""A number that cannot be built or is not what it claims to be.

	A `ValueError` because that is what a caller passing a bad prefix has done,
	and because the tools layer turns one into a refusal the operator reads.
	"""


def check_digit(digits: str) -> int:
	"""The GS1 modulo-10 check digit for a key body, per General Specs §7.9.

	Positions are weighted from the RIGHT, alternating 3 and 1, starting with 3
	on the rightmost character of the body. Weighting from the left instead is
	the classic implementation bug and it is invisible on palindromic test data,
	which is why the tests below check an asymmetric number with a known answer.
	"""
	body = str(digits or "")
	if not body.isdigit():
		raise GS1Error(f"a GS1 key body is digits only; got {body!r}")
	total = sum(int(char) * (3 if index % 2 == 0 else 1) for index, char in enumerate(reversed(body)))
	return (10 - (total % 10)) % 10


def is_well_formed(key: str) -> bool:
	"""Whether a key is all digits, a GS1 length, and carries the right check digit.

	The one function every "is this barcode real" question should ask. It does
	NOT say the key is the farm's — see `describe`.
	"""
	text = str(key or "").strip()
	if len(text) not in KEY_LENGTHS or not text.isdigit():
		return False
	return int(text[-1]) == check_digit(text[:-1])


def validate_gtin(gtin: str) -> bool:
	"""Whether a GTIN is well formed. GTIN-8 included, because scanners read them."""
	text = str(gtin or "").strip()
	return len(text) in (8, *GTIN_LENGTHS) and is_well_formed(text)


def validate_gln(gln: str) -> bool:
	"""Whether a GLN is well formed. Thirteen digits, always."""
	text = str(gln or "").strip()
	return len(text) == GLN_LENGTH and is_well_formed(text)


def validate_sscc(sscc: str) -> bool:
	"""Whether an SSCC is well formed. Eighteen digits, always."""
	text = str(sscc or "").strip()
	return len(text) == SSCC_LENGTH and is_well_formed(text)


def normalise_prefix(prefix) -> str:
	"""A company prefix with the punctuation people type in it removed.

	Operators transcribe a prefix off a GS1 certificate that prints it as
	`0-6141-04` or `061410 4`, so spaces, hyphens and dots come off. Anything
	left that is not a digit is refused rather than stripped: a prefix with a
	letter in it is a custom identifier somebody has put in the GS1 box, and
	silently deleting the letter would build a number for a different company.
	"""
	text = "".join(char for char in str(prefix or "") if not char.isspace() and char not in "-.")
	if not text:
		return ""
	if not text.isdigit():
		raise GS1Error(
			f"a GS1 company prefix is digits only; got {str(prefix)!r}. A prefix with letters in it "
			'belongs in custom mode — pass standard="custom".'
		)
	return text


def prefix_or_raise(prefix, standard: str = "gs1") -> str:
	"""The prefix to build a key from, or an error saying why there is none.

	The whole of the "never invent a prefix" rule from the module docstring lives
	here, so that every generator inherits it by calling this one function.
	"""
	mode = (standard or "gs1").strip().lower()
	if mode not in STANDARDS:
		raise GS1Error(f"standard must be one of {', '.join(STANDARDS)}; got {standard!r}")

	if mode == "custom":
		text = "".join(str(prefix or "").split())
		if not text:
			raise GS1Error(
				"custom mode still needs a prefix — it is what makes the farm's own numbers unique. "
				"Pass one, or configure " + CUSTOM_PREFIX_FIELD + "."
			)
		if not text.isdigit():
			raise GS1Error(
				f"a custom prefix must be digits so the key can be encoded as a barcode; got {text!r}"
			)
		return text

	text = normalise_prefix(prefix)
	if not text:
		raise GS1Error(
			"no GS1 company prefix is configured, and this will not invent one. A generated key "
			"built on a made-up prefix is either unscannable or is another company's product number. "
			'Configure the farm\'s licensed prefix, or use standard="custom" for numbers that are '
			"explicitly the farm's own and not GS1 keys."
		)
	if not 4 <= len(text) <= 11:
		raise GS1Error(
			f"a GS1 company prefix is 4 to 11 digits; got {len(text)} ({text!r}). Check the licence "
			"certificate — the figure printed there sometimes includes a leading indicator digit."
		)
	return text


def generate_gtin(item_reference, prefix, length: int = 13, standard: str = "gs1") -> str:
	"""A GTIN for one trade item, from the farm's prefix and its own reference.

	`item_reference` is the farm's number for the item and is LEFT-PADDED with
	zeroes to fill the key, because that is what GS1 requires and because it is
	what makes reference `7` and reference `007` the same trade item rather than
	two. A reference too long for the remaining room is an error naming both
	figures: with an 8-digit prefix a GTIN-13 leaves four characters, and the
	farm that has more than 9,999 trade items needs a shorter prefix, not a
	truncated number.

	GTIN-14 prepends the indicator digit `0` (a single trade item, not a case
	configuration). A farm that cases its fruit sets the indicator itself by
	building a GTIN-13 and prefixing the digit it means.
	"""
	if length not in GTIN_LENGTHS:
		raise GS1Error(
			f"a GTIN this builds is {', '.join(str(n) for n in GTIN_LENGTHS)} digits; got {length}"
		)
	company = prefix_or_raise(prefix, standard)
	indicator = "0" if length == 14 else ""
	body = (
		indicator + company + _reference(item_reference, length - 1 - len(indicator) - len(company), "item")
	)
	return body + str(check_digit(body))


def generate_gln(location_reference, prefix, standard: str = "gs1") -> str:
	"""A GLN for one physical location — a block, a packing house, a cold room.

	The number a shipment names as its ship-from, and the reason a Field is worth
	giving one: an FSMA traceability record that says "Block 7" is a name only
	this farm can resolve, and one that says a GLN is a location any buyer's
	system can.
	"""
	company = prefix_or_raise(prefix, standard)
	body = company + _reference(location_reference, GLN_LENGTH - 1 - len(company), "location")
	return body + str(check_digit(body))


def generate_sscc(serial_reference, prefix, extension: str = "0", standard: str = "gs1") -> str:
	"""An SSCC for one logistic unit — in this app, one pallet.

	The extension digit is the farm's own, allocated to increase capacity; `0` is
	the conventional default. An SSCC must never be reused for a different
	pallet, which is a constraint on the caller allocating `serial_reference`
	and not something arithmetic can enforce.
	"""
	digit = str(extension or "0")
	if len(digit) != 1 or not digit.isdigit():
		raise GS1Error(f"the SSCC extension is a single digit; got {extension!r}")
	company = prefix_or_raise(prefix, standard)
	body = digit + company + _reference(serial_reference, SSCC_LENGTH - 2 - len(company), "serial")
	return body + str(check_digit(body))


def describe(key: str, prefix="", standard: str = "gs1") -> dict:
	"""What a scanned number is: `{key, length, kind, well_formed, ours, gs1_compliant}`.

	`ours` is whether the key starts with the farm's prefix, and it is a separate
	answer from `well_formed` on purpose. A well-formed GTIN that is not ours is
	a supplier's box that arrived on the farm — a completely normal thing to
	scan, and one a receiving flow must be able to tell apart from a case the
	farm packed.

	`gs1_compliant` is False in custom mode however sound the number is, because
	the one thing a custom key must never do is get quoted to a buyer as a GS1
	key.
	"""
	text = str(key or "").strip()
	mode = (standard or "gs1").strip().lower()
	try:
		company = prefix_or_raise(prefix, mode) if prefix else ""
	except GS1Error:
		company = ""
	kinds = {8: "GTIN-8", 12: "GTIN-12", 13: "GTIN-13 or GLN", 14: "GTIN-14", 18: "SSCC"}
	return {
		"key": text,
		"length": len(text),
		"kind": kinds.get(len(text), "unknown"),
		"well_formed": is_well_formed(text),
		"ours": bool(company) and text.startswith(company),
		"gs1_compliant": mode == "gs1" and is_well_formed(text),
	}


def configured_prefix(standard: str = "gs1") -> str:
	"""The prefix stored on this site, or `""` if the site stores none.

	Answers `""` rather than raising when the settings doctype has no such field,
	which is every site today — see the module docstring. Callers pass the result
	to `prefix_or_raise`, which is where the absence becomes an error with an
	explanation attached.
	"""
	fieldname = CUSTOM_PREFIX_FIELD if (standard or "gs1").strip().lower() == "custom" else PREFIX_FIELD
	try:
		import frappe

		from . import compat, settings

		if not compat.has_field(settings.SETTINGS_DOCTYPE, fieldname):
			return ""
		return str(frappe.db.get_single_value(settings.SETTINGS_DOCTYPE, fieldname) or "").strip()
	except Exception:
		# A prefix lookup must never be the reason a page fails to render. An
		# unreadable setting is the same situation as an unset one, and the
		# generators refuse either way rather than proceeding on a guess.
		return ""


# ── the part nobody outside calls ───────────────────────────────────────────
def _reference(reference, room: int, what: str) -> str:
	"""A reference zero-padded into the room a key leaves for it.

	The error is written out in full because it is the one an operator will
	actually hit, and "invalid length" would leave them measuring digits by
	hand.
	"""
	text = "".join(str(reference if reference is not None else "").split())
	if not text.isdigit():
		raise GS1Error(f"the {what} reference is digits only; got {str(reference)!r}")
	if room < 1:
		raise GS1Error(
			f"the company prefix leaves no room for a {what} reference in this key. A shorter "
			"prefix is issued for a larger allocation — this is a GS1 licence question, not a "
			"software one."
		)
	if len(text) > room:
		raise GS1Error(
			f"the {what} reference {text!r} is {len(text)} digits and this key leaves room for "
			f"{room}. Either the prefix is longer than the licence, or the farm has outgrown the "
			"allocation it was issued."
		)
	return text.rjust(room, "0")
