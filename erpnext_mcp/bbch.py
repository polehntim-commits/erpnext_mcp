# SPDX-License-Identifier: MIT
"""The BBCH phenology scale: reading a code, and offering the ones a crop has.

WHAT A BBCH CODE IS, BECAUSE THE COLUMN DOES NOT SAY. BBCH is the standard
two-digit growth-stage scale used across European and North American agronomy:
the tens digit is the PRINCIPAL stage (0 germination through 9 senescence) and
the units digit is the secondary stage within it. `65` is full flowering, `81`
is the beginning of ripening, `00` is a dry seed. The whole scale is 00-99 and
every code in it is meaningful, which is why a stage column cannot be validated
by "is it a number" — `65` and `56` are both valid and they are four months
apart.

WHY THIS IS A MODULE AND NOT A SELECT FIELD'S OPTIONS. Perennial tree fruit does
not use the same secondary stages as a row crop, and the farm's own scale is
stored per crop — `Crop.bbch_scale` on this app, `Commodity.bbch_scale` on the
farm_app it came from. A Select field would have to carry every code for every
crop, so a cherry block would offer tillering stages. The scale is therefore
DATA, and this module is what turns that data into something a picker can draw
and a validator can check against.

THE OUTPUT SHAPE IS A CONTRACT AND NOT AN IMPLEMENTATION DETAIL. `picker()`
returns `{groups, desc_map, varieties, is_perennial}` with each group holding
`{code, desc, depth}` options, because that is the shape the farm_app's
`/api/refdata` endpoint has published for two seasons and the iOS client parses
into `BBCHOption`. Cycle 3 remaps those routes onto this app; a payload that
changed shape on the way would break the picker on every phone in the field
before anybody noticed the routes had moved. The `desc_map` half is stripped
from mobile payloads — the client derives its own display names from `code` and
`desc` — but web templates read it, so it is built either way.

A CODE THAT DOES NOT PARSE IS UNKNOWN, NEVER AN ERROR. `Crop Observation.
growth_stage_code` is a `Data` column deliberately, so what arrives is `"87"`,
`" 87 "`, `"BBCH 87"` or `"petal fall"`. A scouting round that recorded a Brix
and a pest count and a stage nobody could parse is three quarters of a useful
observation, and refusing the whole record over the stage column throws away the
part that parsed. `parse()` therefore answers `None` rather than raising, and
every caller here treats `None` as "not stated".

RELATED, AND DELIBERATELY SEPARATE: `overlays.bbch_band` maps a code to one of
six harvest-readiness BANDS for the map's colour. That is a display question
with its own table and its own thresholds; this module answers what a code *is*
and which codes a crop offers. They share only the four lines that turn text
into an integer, and merging them would put the map's colour policy inside the
picker's vocabulary.
"""

from __future__ import annotations

#: The principal stages of the generic BBCH scale, in order. Every published
#: crop-specific scale keeps these ten headings and varies the secondary stages
#: underneath them, which is what makes a generic fallback honest rather than a
#: guess: a crop with no scale of its own still gets the right ten groups.
PRINCIPAL_STAGES = (
	("0", "Germination / Dormancy"),
	("1", "Leaf development"),
	("2", "Formation of side shoots / Tillering"),
	("3", "Stem elongation / Rosette growth"),
	("4", "Vegetative parts / Booting"),
	("5", "Inflorescence / Heading"),
	("6", "Flowering"),
	("7", "Development of fruit"),
	("8", "Ripening / Maturity"),
	("9", "Senescence / Dormancy"),
)

#: The generic secondary stages, `{principal: ((code, description), ...)}`. These
#: are the BBCH general scale's own entries — not one farm's opinion — so a site
#: that has never edited a Crop still offers a usable picker on day one.
GENERIC_SECONDARY = {
	"0": (
		("00", "Dry seed"),
		("01", "Seed imbibition"),
		("03", "Seed imbibition complete"),
		("05", "Radicle emerged"),
		("07", "Coleoptile emerged"),
		("09", "Emergence"),
	),
	"1": (
		("10", "First leaf through coleoptile"),
		("11", "First true leaf unfolded"),
		("12", "2 leaves unfolded"),
		("13", "3 leaves unfolded"),
		("19", "9 or more leaves unfolded"),
	),
	"2": (
		("21", "First side shoot visible"),
		("22", "2 side shoots visible"),
		("29", "End of tillering"),
	),
	"3": (
		("31", "First node detectable"),
		("32", "2nd node detectable"),
		("39", "9 or more nodes detectable"),
	),
	"4": (
		("41", "Beginning of booting"),
		("43", "Mid boot"),
		("49", "Flag leaf sheath opening"),
	),
	"5": (
		("51", "Beginning of heading"),
		("55", "Middle of heading"),
		("59", "End of heading"),
	),
	"6": (
		("61", "Beginning of flowering"),
		("65", "Full flowering"),
		("69", "End of flowering"),
	),
	"7": (
		("71", "Watery ripe"),
		("73", "Early milk"),
		("75", "Medium milk"),
		("77", "Late milk"),
	),
	"8": (
		("83", "Early dough"),
		("85", "Soft dough"),
		("87", "Hard dough"),
		("89", "Fully ripe / Harvest maturity"),
	),
	"9": (
		("92", "Over-ripe, grain shattering"),
		("97", "Plant dead"),
		("99", "Harvested product"),
	),
}

#: The separators a stored description may use between a code and its words.
#: Three of them because three are in the data: the farm_app's own seeds wrote an
#: EM DASH, its AI-assisted imports wrote an EN DASH, and hand entry wrote a
#: hyphen. Stripping only one leaves `"87 – Hard dough"` displayed as
#: `"87 — 87 – Hard dough"` in a picker that prepends the code itself.
_CODE_SEPARATORS = (" — ", " – ", " - ")

#: How deep a nested scale is walked before the walk is abandoned. Published
#: BBCH scales nest two levels at most (principal → secondary → mesostage); a
#: scale claiming twenty is a cycle in somebody's JSON, and a recursive walk
#: over a cycle is a hung worker rather than a wrong answer.
MAX_DEPTH = 6

#: What `describe()` says about a code that is not in any scale. Named rather
#: than inlined because two callers compare against it.
UNKNOWN_LABEL = "Unknown stage"


def parse(code) -> int | None:
	"""The integer 0-99 a stage column means, or `None` if it does not state one.

	Accepts what the column actually holds: `87`, `"87"`, `" 87 "`, `"BBCH 87"`,
	`"87 — Hard dough"`. A single digit is a PRINCIPAL stage and reads as itself
	(`"8"` → 8, not 80) because that is how the scale writes it and how the
	pickers below offer it.

	Anything with no digits, more than two digits, or digits that are part of a
	longer number reads as `None`. That last case is the one worth stating: an
	observation whose stage column holds `"2026-08-01"` has a date in it, not a
	stage, and answering `20` for it would be worse than answering nothing.
	"""
	if code is None or isinstance(code, bool):
		return None
	if isinstance(code, int):
		return code if 0 <= code <= 99 else None
	if isinstance(code, float):
		return parse(int(code)) if code == int(code) else None

	text = str(code).strip()
	if not text:
		return None
	# The code is the FIRST run of digits, and any second run disqualifies the
	# whole string: "87 — Hard dough" has one run, "2026-08-01" has three, and a
	# scale label like "BBCH 8" has one after a word.
	runs, current = [], ""
	for char in text:
		if char.isdigit():
			current += char
		elif current:
			runs.append(current)
			current = ""
	if current:
		runs.append(current)
	if len(runs) != 1:
		return None
	digits = runs[0]
	if len(digits) > 2:
		return None
	number = int(digits)
	return number if 0 <= number <= 99 else None


def is_valid(code) -> bool:
	"""Whether a stage column states a code inside the 00-99 scale."""
	return parse(code) is not None


def principal(code) -> int | None:
	"""The principal stage (0-9) a code sits in, or `None`.

	A single-digit code IS its principal stage; a two-digit one is its tens
	digit. `9` and `92` both answer 9.
	"""
	number = parse(code)
	if number is None:
		return None
	return number // 10 if number >= 10 else number


def normalise(code) -> str:
	"""The canonical two-character spelling of a code, or `""`.

	`"BBCH 7"` → `"07"`, `7` → `"07"`, `"65"` → `"65"`. Two characters always,
	because the code is a two-digit scale and `"7"` sorts before `"65"` in every
	string comparison a report will make of it.
	"""
	number = parse(code)
	return "" if number is None else f"{number:02d}"


def strip_code_prefix(text) -> str:
	"""A stage description with any leading `NN — ` removed.

	Stored scales are inconsistent about whether the description repeats the
	code, because two seasons of hand entry and one AI-assisted import each
	chose differently. Everything downstream prepends the code itself, so the
	repetition has to come off here or it is displayed twice.
	"""
	raw = str(text or "").strip()
	if not raw:
		return ""
	for separator in _CODE_SEPARATORS:
		if separator in raw:
			return raw.split(separator, 1)[-1].strip()
	return raw


def generic_scale() -> dict:
	"""The generic BBCH scale in the stored `bbch_scale` shape.

	Built from the two tables above rather than written out a second time: a
	fallback that drifted from the constants it is meant to mirror would be a
	picker offering stages no validator accepts.
	"""
	return {
		"category": "plant",
		"is_perennial": False,
		"stages": [
			{
				"code": principal_code,
				"desc": f"{principal_code} — {label}",
				"children": {
					child_code: {"desc": f"{child_code} — {child_label}"}
					for child_code, child_label in GENERIC_SECONDARY.get(principal_code, ())
				},
			}
			for principal_code, label in PRINCIPAL_STAGES
		],
		"varieties": [],
	}


def picker(scale=None) -> dict:
	"""`{groups, desc_map, varieties, is_perennial}` for a crop's stored scale.

	The shape the iOS client and the web templates both parse — see the module
	docstring, which is where the reason it may not change is written down.

	`scale` is a crop's stored `bbch_scale` dict. `None`, a non-dict, or a dict
	with no usable stages all fall back to the generic scale, because a picker
	that renders empty is indistinguishable to the user from a broken form.

	Children arrive as either a dict keyed by code or a list of objects with a
	`code` key — the farm_app wrote both, a season apart, and both are still in
	the data. Both are read; neither is normalised on the way in, because
	rewriting an operator's stored JSON as a side effect of drawing a dropdown
	is a migration disguised as a render.
	"""
	if not isinstance(scale, dict):
		scale = generic_scale()

	raw_stages = scale.get("stages")
	if not isinstance(raw_stages, list) or not _usable(raw_stages):
		fallback = generic_scale()
		raw_stages = fallback["stages"]
		# `is_perennial` and `varieties` are the crop's own facts even when its
		# stage list is unusable, so only the stages fall back.
		scale = {**scale, "stages": raw_stages} if scale else fallback

	desc_map: dict = {}
	groups = []
	for stage in sorted(
		(item for item in raw_stages if isinstance(item, dict) and "code" in item),
		key=lambda item: str(item.get("code")),
	):
		stage_code = str(stage["code"])
		stage_desc = strip_code_prefix(stage.get("desc") or stage.get("description")) or "Unnamed stage"
		desc_map[stage_code] = f"{stage_code} — {stage_desc}"
		options = [{"code": stage_code, "desc": stage_desc, "depth": 0}]
		options.extend(_children(stage, desc_map, depth=0))
		groups.append({"label": f"{stage_code} — {stage_desc}", "options": options})

	return {
		"groups": groups,
		"desc_map": desc_map,
		"varieties": _varieties(scale.get("varieties")),
		"is_perennial": bool(scale.get("is_perennial", False)),
	}


def codes_in(scale=None) -> tuple:
	"""Every code a scale offers, canonical two-character spelling, sorted.

	What a validator checks against, and what a migration compares two scales
	with. Single-digit principal codes normalise to `"00"`-`"90"`, so a stored
	`"7"` and a stored `"07"` are one entry rather than two.
	"""
	found = set()
	for group in picker(scale)["groups"]:
		for option in group["options"]:
			spelling = normalise(option["code"])
			if spelling:
				found.add(spelling)
	return tuple(sorted(found))


def describe(code, scale=None) -> str:
	"""The words for a code — the crop's own if it has them, generic if not.

	A code the crop's scale does not carry still describes, from the generic
	scale, because a stage recorded before somebody trimmed the crop's scale is
	a historical fact and a report of it that says "Unknown stage" has lost
	information the record still holds.
	"""
	spelling = normalise(code)
	if not spelling:
		return UNKNOWN_LABEL

	for candidate in (scale, generic_scale()):
		if candidate is None:
			continue
		lookup = picker(candidate)["desc_map"]
		for key in (spelling, spelling.lstrip("0") or "0"):
			if key in lookup:
				return lookup[key]
	return UNKNOWN_LABEL


def validate(code, scale=None) -> dict:
	"""`{"code", "valid", "in_scale", "label", "principal", "reason"}` for a code.

	Two separate questions, answered separately on purpose. `valid` is whether
	the code is in the 00-99 scale at all; `in_scale` is whether this crop
	offers it. A cherry observation recording `29` (end of tillering) is a valid
	BBCH code and a nonsense cherry stage, and a caller that wants to warn about
	the second without refusing the first needs both answers.
	"""
	number = parse(code)
	spelling = normalise(code)
	if number is None:
		return {
			"code": None,
			"valid": False,
			"in_scale": False,
			"label": UNKNOWN_LABEL,
			"principal": None,
			"reason": "not a BBCH code — expected two digits, 00 to 99",
		}

	offered = codes_in(scale)
	in_scale = spelling in offered
	return {
		"code": spelling,
		"valid": True,
		"in_scale": in_scale,
		"label": describe(spelling, scale),
		"principal": principal(spelling),
		"reason": "" if in_scale else "valid BBCH code, but this crop's scale does not offer it",
	}


def water_management(scale=None, crop_coefficients=None) -> dict:
	"""Per-stage irrigation parameters derived from a scale and its Kc figures.

	`{"stages": {code: {kc, mad, root_depth_mm, critical, notes}}, "defaults":
	{...}}`. The farm_app derived this to drive its irrigation schedule and the
	derivation is the part worth keeping: an operator supplies a Kc per stage
	and a whole-crop MAD and rooting depth, and the three per-stage figures fall
	out of the stage NUMBER.

	THE THREE RULES, EACH OF WHICH IS AN AGRONOMIC CLAIM AND NOT A CONSTANT.

	*Rooting depth grows with the stage.* A germinating seed has roots in the top
	few centimetres and a mature tree has them at the full profile, so depth
	interpolates linearly from ~4% of the crop's maximum at the first stage with
	a Kc to 100% at the last. Irrigating a seedling to a mature root depth is how
	a nursery block gets drowned.

	*Bloom and fruit set tighten the allowable depletion.* Stages 60-79 are
	flowering and fruit development, where water stress costs fruit that cannot
	be recovered later in the season, so MAD is capped at 0.35 there regardless
	of what the crop-wide figure says.

	*Establishment and senescence loosen it.* Below stage 20 the crop is cheap to
	stress and expensive to overwater; at 80 and above the fruit is sizing down
	and the block is heading for dormancy. Both get more slack, capped so that
	"more slack" never becomes "no schedule".

	A crop with no `kc_by_stage` gets empty stages and its defaults, which is the
	honest answer: without a Kc there is no per-stage schedule to derive, and
	inventing one would be a number an operator would irrigate on.
	"""
	coefficients = crop_coefficients if isinstance(crop_coefficients, dict) else {}
	kc_by_stage = coefficients.get("kc_by_stage")
	kc_by_stage = kc_by_stage if isinstance(kc_by_stage, dict) else {}
	global_mad = _float(coefficients.get("mad"), 0.5)
	global_root = _float(coefficients.get("root_depth_mm"), 600.0)
	defaults = {"kc": 1.0, "mad": global_mad, "root_depth_mm": global_root}

	ordered = sorted(
		((code, kc) for code, kc in kc_by_stage.items() if parse(code) is not None),
		key=lambda pair: parse(pair[0]),
	)
	if not ordered:
		return {"stages": {}, "defaults": defaults}

	descriptions = picker(scale)["desc_map"] if scale is not None else {}
	minimum_root = max(50.0, global_root * 0.04)
	span = len(ordered) - 1

	stages = {}
	for index, (code, kc) in enumerate(ordered):
		number = parse(code)
		fraction = (index / span) if span else 1.0
		critical = 60 <= number <= 79
		if critical:
			mad = round(min(global_mad, 0.35), 2)
		elif number < 20:
			mad = round(min(global_mad + 0.2, 0.7), 2)
		elif number >= 80:
			mad = round(min(global_mad + 0.1, 0.6), 2)
		else:
			mad = round(global_mad, 2)

		words = strip_code_prefix(descriptions.get(normalise(code), ""))
		if not words:
			words = strip_code_prefix(describe(code, scale))
			if words == UNKNOWN_LABEL:
				words = ""
		stages[str(code)] = {
			"kc": _float(kc, 1.0),
			"mad": mad,
			"root_depth_mm": int(minimum_root + fraction * (global_root - minimum_root)),
			"critical": critical,
			"notes": _stage_note(words, number, critical),
		}
	return {"stages": stages, "defaults": defaults}


# ── the parts nobody outside calls ──────────────────────────────────────────
def _usable(stages) -> bool:
	"""Whether a stored stage list has anything a picker could draw."""
	return any(isinstance(item, dict) and "code" in item for item in stages)


def _children(stage: dict, desc_map: dict, depth: int) -> list:
	"""Every descendant of a stage, flattened, each carrying its nesting depth.

	Depth is carried rather than derived because the picker INDENTS by it, and a
	mesostage displayed flush against its own parent is a list the reader cannot
	group by eye.
	"""
	if depth >= MAX_DEPTH:
		return []
	children = stage.get("children")
	options = []

	if isinstance(children, dict):
		items = [
			(str(code), child)
			for code, child in sorted(children.items(), key=lambda pair: (len(str(pair[0])), str(pair[0])))
			if isinstance(child, dict)
		]
	elif isinstance(children, list):
		items = [
			(str(child["code"]), child)
			for child in sorted(
				(item for item in children if isinstance(item, dict) and "code" in item),
				key=lambda item: str(item.get("code")),
			)
		]
	else:
		return []

	for code, child in items:
		words = strip_code_prefix(child.get("desc") or child.get("description")) or "Unnamed stage"
		desc_map[code] = f"{code} — {words}"
		options.append({"code": code, "desc": words, "depth": depth})
		options.extend(_children(child, desc_map, depth + 1))
	return options


def _varieties(raw) -> list:
	"""`[{"name", "display"}]` for the varieties a scale names, unnamed ones out.

	A variety with no name cannot be chosen and cannot be matched against a
	block's `variety` column, so it is not offered — an unnamed row in a picker
	is a row somebody selects once and never explains.
	"""
	if not isinstance(raw, list):
		return []
	out = []
	for item in raw:
		if not isinstance(item, dict):
			continue
		name = str(item.get("name") or "").strip()
		if not name:
			continue
		words = str(item.get("description") or "").strip()
		out.append({"name": name, "display": f"{name} — {words}" if words else name})
	return out


def _stage_note(words: str, number: int, critical: bool) -> str:
	"""The irrigation note for a stage — its own words plus what the number means."""
	if critical:
		return f"{words} — critical water demand period" if words else "Critical water demand period"
	if number < 10:
		return f"{words} — establishment phase" if words else "Establishment phase"
	if number >= 90:
		return f"{words} — reduce irrigation" if words else "Reduce irrigation for dormancy/senescence"
	return words


def _float(value, fallback: float) -> float:
	"""A float, or the fallback — including for the string a JSON field holds.

	Not `float(value or fallback)`: a stored MAD of `0` is an operator saying
	"never let this dry down", and reading it as 0.5 would irrigate on somebody
	else's number.
	"""
	if value is None or isinstance(value, bool):
		return fallback
	try:
		return float(value)
	except (TypeError, ValueError):
		return fallback
