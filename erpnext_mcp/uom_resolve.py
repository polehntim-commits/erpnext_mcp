# SPDX-License-Identifier: MIT
"""From the words on a label to a unit this site actually has.

v0.197.0. OML App Feedback AFB-2026-00023: PROWLER rodent bait was refused
because the unit it arrived with was 'Noi' — the first letters of "Norway rats",
the TARGET of the rate, read as if it were the unit. The rate on that label is
"Norway rats: 1 or 2 blocks of bait; roof rats: …", and the unit is the words
AFTER the quantity ("blocks of bait" → Block), never the words before it.

THE ANSWER IS A RESOLUTION, NEVER A REFUSAL. This module does not raise. It says
what it read, which site UOM that is (or `None`), and which units a person could
pick instead. Whether an unresolved unit refuses the call is the CALLER's
decision — `create_item` registers the product anyway and flags it, because the
product on the shelf exists whether or not the unit list knows its unit;
`update_item` refuses, because there a person picked a unit and should be told
it is not one. The shape is frozen in `docs/design/uom_registry.md` §1: the phone
decodes it.

A UNIT IS ONLY EVER ONE THE SITE HAS, AND HAS ENABLED. The alias table maps
label shorthand to CANDIDATE names in preference order, and the first that
exists on this site wins — ERPNext's own list says "Gallon Liquid (US)" where
this app seeds "Gallon", and which of them a site has is not a thing to guess.
"""

from __future__ import annotations

import re

import frappe

from . import ag_uom
from .compat import doctype_exists

UOM = "UOM"

#: How many candidates a person is offered. Enough to pick from on a phone, few
#: enough that the right one is not lost in ERPNext's two hundred units.
CANDIDATE_CAP = 10

#: Label shorthand → site UOM names, best first. Keys are normalised (lower
#: case, no full stops, single spaces). A key maps to several names because the
#: same unit is spelled differently by this app's seed and by ERPNext's list.
ALIASES: dict[str, tuple[str, ...]] = {
	"block": ("Block",),
	"bait block": ("Block",),
	# v0.202.0. The other bait forms (see `ag_uom.BAIT_UNITS`). "pack" is here
	# because Tim asked for it: on a bait label a "pack" is the place pac.
	"place pac": ("Place Pac",),
	"pac": ("Place Pac",),
	"pack": ("Place Pac",),
	"pouch": ("Pouch",),
	"packet": ("Pouch",),
	"bait station": ("Bait Station",),
	"station": ("Bait Station",),
	"blocks of bait": ("Block",),
	"block of bait": ("Block",),
	"oz": ("Ounce",),
	"ounce": ("Ounce",),
	"oz wt": ("Ounce",),
	"fl oz": ("Fluid Ounce", "Fluid Ounce (US)"),
	"fluid ounce": ("Fluid Ounce", "Fluid Ounce (US)"),
	"fl ounce": ("Fluid Ounce", "Fluid Ounce (US)"),
	"lb": ("Pound", "Lb"),
	"lbs": ("Pound", "Lb"),
	"pound": ("Pound", "Lb"),
	"gal": ("Gallon", "Gallon Liquid (US)"),
	"gallon": ("Gallon", "Gallon Liquid (US)"),
	"pt": ("Pint", "Pint, Liquid (US)"),
	"pint": ("Pint", "Pint, Liquid (US)"),
	"qt": ("Quart", "Quart Liquid (US)"),
	"quart": ("Quart", "Quart Liquid (US)"),
	"kg": ("Kg", "Kilogram"),
	"kilogram": ("Kg", "Kilogram"),
	"g": ("Gram",),
	"gram": ("Gram",),
	"ml": ("Millilitre",),
	"milliliter": ("Millilitre",),
	"l": ("Litre",),
	"liter": ("Litre",),
	"litre": ("Litre",),
	"each": ("Nos",),
	"ea": ("Nos",),
	"count": ("Nos",),
	"ac": ("Acre",),
	"acre": ("Acre",),
	"bin": ("Bin",),
	"lug": ("Lug",),
	"bucket": ("Bucket",),
	"bushel": ("Bushel",),
	"bu": ("Bushel",),
	"ton": ("Ton",),
}

#: Words that end a unit phrase. "1 block PER station", "2 lb FOR each acre":
#: the unit is what comes before them.
_STOP_WORDS = frozenset({"per", "for", "every", "in", "at", "and", "each", "when", "if", "to", "a", "an"})

_NUMBER = r"\d+(?:\.\d+)?|\.\d+"
#: A quantity is a number or a range. "or" is a range on a bait label — "1 or 2
#: blocks" means anywhere from one to two, which is how a person reads it.
_QUANTITY = re.compile(
	rf"(?P<low>{_NUMBER})(?:\s*(?:-|–|—|to|or)\s*(?P<high>{_NUMBER}))?"
	r"\s*(?P<unit>[A-Za-z][A-Za-z.]*(?:\s+[A-Za-z][A-Za-z.]*){0,3})?",
	re.IGNORECASE,
)


def _norm(text: str) -> str:
	return " ".join(str(text or "").lower().replace(".", " ").split())


def _singular(word: str) -> str:
	if len(word) > 3 and word.endswith("es") and word[-3] in "sxz":
		return word[:-2]
	if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
		return word[:-1]
	return word


def site_uoms() -> list[dict]:
	"""Every ENABLED UOM on this site as `{name, must_be_whole_number}`."""
	if not doctype_exists(UOM):
		return []
	from .compat import has_field

	with_aliases = has_field(UOM, ALIAS_FIELD)
	fields = ["name", "must_be_whole_number", *([ALIAS_FIELD] if with_aliases else [])]
	rows = frappe.db.get_all(UOM, filters={"enabled": 1}, fields=fields, order_by="name asc", limit=5000)
	return [
		{
			"name": row["name"],
			"must_be_whole_number": bool(int(row.get("must_be_whole_number") or 0)),
			"aliases": parse_aliases(row.get(ALIAS_FIELD)) if with_aliases else [],
		}
		for row in rows
	]


#: v0.202.0. The UOM column a site keeps its own spellings in, one per line —
#: "pacs" for Place Pac was the first. Read by every resolution, written by
#: `tools/uoms.set_uom_aliases`, so a new spelling on a label needs no deploy.
ALIAS_FIELD = "uom_aliases"


def parse_aliases(raw) -> list[str]:
	"""A `uom_aliases` value as a list of distinct spellings, in the order written."""
	out: list[str] = []
	for line in str(raw or "").replace(",", "\n").splitlines():
		spelling = " ".join(line.split())
		if spelling and spelling.lower() not in {entry.lower() for entry in out}:
			out.append(spelling)
	return out


def _phrase_after_quantity(clause: str) -> tuple[str, dict | None]:
	"""`(unit phrase, quantity)` from the first quantity in `clause`.

	THE PHRASE IS WHAT FOLLOWS THE NUMBER. Everything before it is the target —
	a crop or a pest — and reading a unit out of it is exactly the 'Noi' bug.
	"""
	match = _QUANTITY.search(clause)
	if not match:
		return "", None
	low = float(match.group("low"))
	high = float(match.group("high")) if match.group("high") else low
	quantity = {"min": min(low, high), "max": max(low, high)}
	words = []
	for word in (match.group("unit") or "").split():
		if _norm(word) in _STOP_WORDS:
			break
		words.append(word.strip("."))
	return " ".join(w for w in words if w), quantity


def _index(uoms: list[dict]) -> tuple[dict, dict, dict]:
	exact = {row["name"]: row["name"] for row in uoms}
	folded = {}
	for row in uoms:
		folded.setdefault(_norm(row["name"]), row["name"])
	# A site's own spellings, singular and as written. A name always beats an
	# alias: `folded` is consulted first in `_match`.
	site_aliases: dict = {}
	for row in uoms:
		for spelling in row.get("aliases") or ():
			key = _norm(spelling)
			parts = key.split()
			for variant in (key, " ".join([*parts[:-1], _singular(parts[-1])]) if parts else key):
				site_aliases.setdefault(variant, row["name"])
	return exact, folded, site_aliases


def _match(
	phrase: str, exact: dict, folded: dict, site_aliases: dict | None = None
) -> tuple[str | None, str | None]:
	"""The site UOM `phrase` names, trying the longest reading first.

	"blocks of bait" is tried whole, then "blocks of", then "blocks" — so an
	alias written for the whole phrase wins, and a label that adds words after
	the unit ("2 lb product") still finds it.
	"""
	words = phrase.split()
	for size in range(min(len(words), 4), 0, -1):
		part = " ".join(words[:size])
		key = _norm(part)
		if part in exact:
			return exact[part], "exact"
		if key in folded:
			return folded[key], "case"
		if site_aliases and key in site_aliases:
			return site_aliases[key], "site_alias"
		for name in ALIASES.get(key, ()):
			if name in exact:
				return name, "alias"
		parts = key.split()
		singular = " ".join([*parts[:-1], _singular(parts[-1])]) if parts else key
		if singular != key:
			if singular in folded:
				return folded[singular], "plural"
			if site_aliases and singular in site_aliases:
				return site_aliases[singular], "site_alias"
			for name in ALIASES.get(singular, ()):
				if name in exact:
					return name, "plural"
	return None, None


def _candidates(phrase: str, uoms: list[dict], resolved: str | None) -> list[str]:
	"""What a person might have meant: the resolved unit, near spellings, then the farm's own units."""
	names = [row["name"] for row in uoms]
	picked: list[str] = [resolved] if resolved else []
	key = _norm(phrase)
	if key:
		stem = _singular(key.split()[0])
		head = key[:2]
		for name in names:
			folded = _norm(name)
			if stem and (folded.startswith(stem) or stem in folded.split()):
				picked.append(name)
		for name in names:
			if head and _norm(name).startswith(head):
				picked.append(name)
	seeded = {spec["uom_name"] for spec in ag_uom.SEED_UOMS}
	picked.extend(name for name in names if name in seeded or name == "Nos")
	unique: list[str] = []
	for name in picked:
		if name not in unique:
			unique.append(name)
	return unique[:CANDIDATE_CAP]


def _answer(text: str, phrase: str, quantity, uom, matched_by, uoms: list[dict]) -> dict:
	if uom:
		status = "resolved"
	elif phrase:
		status = "unresolved"
	else:
		status = "absent"
	return {
		"text": text,
		"phrase": phrase or None,
		"quantity": quantity,
		"uom": uom,
		"matched_by": matched_by,
		"status": status,
		"candidates": _candidates(phrase, uoms, uom) if status != "absent" else [],
	}


def resolve_unit(text, uoms: list[dict] | None = None) -> dict:
	"""A unit NAMED on its own — a stock unit, a picked rate unit — as a Resolution."""
	uoms = site_uoms() if uoms is None else uoms
	phrase = " ".join(str(text or "").split())
	exact, folded, site_aliases = _index(uoms)
	uom, how = _match(phrase, exact, folded, site_aliases) if phrase else (None, None)
	return _answer(phrase, phrase, None, uom, how, uoms)


def resolve_rate(text, uoms: list[dict] | None = None) -> dict:
	"""A label rate — one clause per crop or pest — as a Resolution with its clauses.

	The first clause that resolves decides `uom`. `mixed` is true when the
	clauses name different units, which a person should look at: one product
	measured two ways is usually a misread, and occasionally a label that really
	does give a dry and a liquid rate.
	"""
	uoms = site_uoms() if uoms is None else uoms
	raw = str(text or "").strip()
	exact, folded, site_aliases = _index(uoms)
	clauses = []
	for piece in re.split(r"[;\n]+", raw):
		piece = piece.strip()
		if not piece:
			continue
		target, _, rest = piece.rpartition(":")
		body = rest if target else piece
		phrase, quantity = _phrase_after_quantity(body)
		uom, how = _match(phrase, exact, folded, site_aliases) if phrase else (None, None)
		clauses.append(
			{
				"target": target.strip() or None,
				"phrase": phrase or None,
				"quantity": quantity,
				"uom": uom,
				"matched_by": how,
			}
		)
	first = next((c for c in clauses if c["uom"]), None) or next((c for c in clauses if c["phrase"]), None)
	answer = _answer(
		raw,
		(first or {}).get("phrase") or "",
		(first or {}).get("quantity"),
		(first or {}).get("uom"),
		(first or {}).get("matched_by"),
		uoms,
	)
	answer["clauses"] = [{k: v for k, v in c.items() if k != "matched_by"} for c in clauses]
	answer["mixed"] = len({c["uom"] for c in clauses if c["uom"]}) > 1
	return answer
