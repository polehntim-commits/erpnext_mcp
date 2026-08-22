# SPDX-License-Identifier: MIT
"""Worker PII: taking it out of anything that leaves the farm, and proving it is out.

THE PRINCIPLE, WHICH IS OLDER THAN THIS APP. Individualised worker production
data — who picked what, how fast, out of which row — is employment information.
It is exactly what the farm needs internally to run a crew and pay people
correctly, and it is exactly what must never reach a buyer, a processor, an
auditor's third-party portal, or a public traceability record. A packing house
that can see which of a farm's workers is slowest has been handed something it
has no business with, and the farm handed it over as a side effect of proving
its fruit was traceable.

THIS IS NOT THE ROLE GATE AND DOES NOT REPLACE IT. `permissions.py` decides who
may READ a register; this decides what survives into a document that is being
EXPORTED. The two are different questions with different failure modes: a role
gate failing means the wrong person opened a form, and this failing means the
right person sent a correct report to a customer with a crew's names in it. A
report generator that ran with a Farm Manager's permissions — as every scheduled
export does — passes every role check on its way to disclosing everything.

WHAT COMES OUT, AND THE ONE AGGREGATE THAT GOES WITH IT. Direct identifiers
obviously: employee ids, names, badge numbers, the Frappe user behind them.
Less obviously `total_piece_units` and any bare crew count, because on a small
farm an aggregate over three people is three people — the farm_app's own note
on this is right, and it is the rule most redaction code gets wrong.

WHAT STAYS, AND WHY IT MUST. Hex-level and block-level production, quantities,
grades, temperatures, dates, lot and pallet ids, growth stages. All of it is
geographic or physical rather than personal, and all of it is the actual
substance of a traceability record. A stripper that took the quantity out
because it appeared next to a worker's name would produce a document that
protects the crew by being useless, and the farm would go back to sending the
spreadsheet.

PSEUDONYMS ARE OFFERED BECAUSE DELETION SOMETIMES LOSES A REAL FACT. "Four
pickers worked this block over two days" is legitimate context for a food-safety
investigation and carries no identity. `strip(..., pseudonyms=True)` replaces
each identifier with a stable `Worker 1`, `Worker 2` within the ONE document
being exported — never across documents, because a label that stayed stable
across a season's exports is an identifier again, just one the reader has to
work slightly harder to resolve.

`audit()` IS THE HALF THAT MAKES THIS TRUSTWORTHY. A stripper nobody can check
is a promise. `audit()` walks a payload and reports every path that WOULD be
removed, so a test can assert a real export is clean and a reviewer can see what
a change to the key list actually catches.
"""

from __future__ import annotations

#: Keys removed outright wherever they appear. Exact matches, casefolded.
#: `total_piece_units` is here for the reason in the module docstring: on a crew
#: of three it is not an aggregate.
PII_KEYS = frozenset(
	{
		"employee",
		"employee_id",
		"employee_name",
		"employee_number",
		"worker",
		"worker_id",
		"worker_name",
		"harvester_name",
		"packer_name",
		"picker_name",
		"applicator_name",
		"badge_id",
		"badge_number",
		"mobile_user",
		"user_id",
		"first_name",
		"last_name",
		"full_name",
		"personal_email",
		"cell_number",
		"phone",
		"date_of_birth",
		"ssn",
		"tax_id",
		"worker_production",
		"total_piece_units",
		"crew_size",
		"headcount",
	}
)

#: Substrings that make a key personal whatever it is called. Matched against
#: the casefolded key, so `spray_applicator`, `crew_member_names` and
#: `modified_by_employee` all go without needing to be listed.
PII_FRAGMENTS = (
	"employee",
	"worker",
	"picker",
	"harvester",
	"applicator",
	"crew_member",
	"_by_user",
	"badge",
	"ssn",
	"national_id",
)

#: Frappe's own audit columns. They name a person on every row of every doctype,
#: so they leave any exported document — an outward record that says who touched
#: it is an outward record naming staff.
FRAMEWORK_KEYS = frozenset({"owner", "modified_by", "_assign", "_liked_by", "_comments"})

#: Keys that LOOK personal to the fragment rules above and are not. Checked
#: before the fragments, because losing them silently produces a report that is
#: wrong rather than one that is redacted.
KEEP_KEYS = frozenset(
	{
		"employee_count",  # a Market Participant's size — a competitor's public figure
		"worker_protection_standard",  # 40 CFR §170, a regulation's name
		"applicator_license_required",  # a boolean about the work, not the person
	}
)

#: What replaces a removed value when the caller asked for a placeholder rather
#: than a deletion, and the label pattern pseudonyms use.
REDACTED = "[redacted]"
PSEUDONYM = "Worker {index}"
STAFF_PSEUDONYM = "Staff {index}"

#: How deep the walk goes before it stops. A traceability payload nests four or
#: five levels; a hundred is a cycle somebody built with a shared dict, and a
#: recursive walk over one never returns.
MAX_DEPTH = 40


def is_pii_key(key) -> bool:
	"""Whether a key names worker PII, by exact match then by fragment."""
	text = str(key or "").strip().casefold()
	if not text or text in KEEP_KEYS:
		return False
	if text in PII_KEYS or text in FRAMEWORK_KEYS:
		return True
	return any(fragment in text for fragment in PII_FRAGMENTS)


def strip(payload, pseudonyms: bool = False, extra_keys=(), _state=None, _depth=0):
	"""A copy of a payload with worker PII removed. The original is not modified.

	Dicts, lists and tuples are walked; everything else is returned as it stands.
	A list ITEM that is itself a worker record — a dict whose keys are mostly
	identifiers — is dropped whole rather than emptied, because a list of empty
	dicts still discloses the crew size the module docstring says must go.

	With `pseudonyms=True` those items survive with their identity replaced by a
	stable label for this call only. See the module docstring on why the
	stability stops at the document boundary.
	"""
	state = _state if _state is not None else {"names": {}, "next": 1}
	extra = frozenset(str(key).strip().casefold() for key in extra_keys or ())

	if _depth >= MAX_DEPTH:
		return REDACTED
	if isinstance(payload, dict):
		# ONE RECORD IS ONE PERSON, so every identity field in this dict resolves
		# to the same label. Keying the label on each VALUE instead would print a
		# row whose `employee` is Worker 1 and whose `employee_name` is Worker 2 —
		# no disclosure, but a reader counting labels would count the crew twice.
		token = _record_token(payload)
		out = {}
		for key, value in payload.items():
			casefolded = str(key).strip().casefold()
			if casefolded in extra or is_pii_key(key):
				if pseudonyms and _is_identifier(key):
					out[key] = _pseudonym(token if token is not None else value, state, key)
				continue
			out[key] = strip(value, pseudonyms, extra_keys, state, _depth + 1)
		return out
	if isinstance(payload, (list, tuple)):
		items = []
		for item in payload:
			if isinstance(item, dict) and _is_worker_record(item, extra):
				if not pseudonyms:
					continue
			items.append(strip(item, pseudonyms, extra_keys, state, _depth + 1))
		return type(payload)(items) if isinstance(payload, tuple) else items
	return payload


def audit(payload, extra_keys=(), _prefix: str = "", _depth: int = 0) -> list:
	"""Every path `strip` would remove, as `[{"path", "key", "reason"}]`.

	The proof half of this module — see the docstring. Paths read like
	`buckets[0].employee_name`, so a failing assertion in a test names the field
	and not merely the fact that something was found.
	"""
	extra = frozenset(str(key).strip().casefold() for key in extra_keys or ())
	found = []
	if _depth >= MAX_DEPTH:
		return [{"path": _prefix, "key": "", "reason": f"nesting deeper than {MAX_DEPTH} levels"}]
	if isinstance(payload, dict):
		for key, value in payload.items():
			path = f"{_prefix}.{key}" if _prefix else str(key)
			casefolded = str(key).strip().casefold()
			if casefolded in extra:
				found.append({"path": path, "key": str(key), "reason": "named by the caller"})
			elif is_pii_key(key):
				found.append({"path": path, "key": str(key), "reason": _why(key)})
			else:
				found.extend(audit(value, extra_keys, path, _depth + 1))
	elif isinstance(payload, (list, tuple)):
		for index, item in enumerate(payload):
			found.extend(audit(item, extra_keys, f"{_prefix}[{index}]", _depth + 1))
	return found


def is_clean(payload, extra_keys=()) -> bool:
	"""Whether a payload already carries no worker PII. What a test asserts."""
	return not audit(payload, extra_keys)


def redact_row(row, extra_keys=()) -> dict:
	"""One flat export row with its PII columns removed rather than blanked.

	For CSV writers, which take a dict per row. The columns are REMOVED and not
	set to `""`, because a header row naming `employee_name` in a file that is
	supposed to have no worker data in it is a question the farm should not have
	to answer twice.
	"""
	if not isinstance(row, dict):
		return row
	extra = frozenset(str(key).strip().casefold() for key in extra_keys or ())
	return {
		key: value
		for key, value in row.items()
		if str(key).strip().casefold() not in extra and not is_pii_key(key)
	}


def safe_columns(columns, extra_keys=()) -> list:
	"""The subset of a column list that may leave the farm, order preserved."""
	extra = frozenset(str(key).strip().casefold() for key in extra_keys or ())
	return [
		column
		for column in columns or []
		if str(column).strip().casefold() not in extra and not is_pii_key(column)
	]


# ── the parts nobody outside calls ──────────────────────────────────────────
def _why(key) -> str:
	text = str(key or "").strip().casefold()
	if text in FRAMEWORK_KEYS:
		return "a Frappe audit column naming the staff member who touched the row"
	if text in ("total_piece_units", "crew_size", "headcount"):
		return "an aggregate that identifies a crew on a farm this size"
	if text in PII_KEYS:
		return "a direct worker identifier"
	return "a key whose name says it carries worker data"


def _is_identifier(key) -> bool:
	"""Whether a key is a worker's IDENTITY rather than data attached to one.

	Only identities take a pseudonym. `worker_production` is a list of records
	and giving it the label `Worker 3` would replace data with a name.
	"""
	text = str(key or "").strip().casefold()
	return text in {
		"employee",
		"employee_id",
		"employee_name",
		"employee_number",
		"worker",
		"worker_id",
		"worker_name",
		"harvester_name",
		"packer_name",
		"picker_name",
		"applicator_name",
		"full_name",
		"badge_id",
		"badge_number",
		"mobile_user",
		"owner",
		"modified_by",
	}


def _is_worker_record(item: dict, extra: frozenset) -> bool:
	"""Whether a dict in a list IS a worker rather than merely mentioning one.

	The test is whether it carries an identity key at all. A bucket entry with an
	`employee` on it is a worker's production record and goes; a shipment line
	that happens to carry `owner` is not — which is why `owner` alone does not
	qualify a record, only a named worker field does.
	"""
	for key in item:
		text = str(key).strip().casefold()
		if text in FRAMEWORK_KEYS:
			continue
		if text in extra or (is_pii_key(key) and _is_identifier(key)):
			return True
	return False


def _record_token(record: dict):
	"""The value that identifies the person a dict is about, or `None`.

	The FIRST identity field in the dict's own order, framework columns last: a
	bucket entry carries both an `employee` and an `owner`, and the crew member
	the row is about is the employee — the owner is whichever office account
	synced it.
	"""
	fallback = None
	for key, value in record.items():
		if not _is_identifier(key) or value in (None, ""):
			continue
		if str(key).strip().casefold() in FRAMEWORK_KEYS:
			fallback = fallback if fallback is not None else value
			continue
		return value
	return fallback


def _pseudonym(value, state: dict, key="") -> str:
	"""A stable label for this call. Same person, same label, within one document.

	Framework audit columns get `Staff N` rather than `Worker N`, because the
	account that last saved a row is usually an office user and calling them a
	worker would put a person in the crew who never picked anything.
	"""
	pattern = STAFF_PSEUDONYM if str(key).strip().casefold() in FRAMEWORK_KEYS else PSEUDONYM
	token = f"{pattern}\x00{value}"
	if token not in state["names"]:
		state["names"][token] = pattern.format(index=state["next"])
		state["next"] += 1
	return state["names"][token]
