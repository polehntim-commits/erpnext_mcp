# SPDX-License-Identifier: MIT
"""Maximum residue limits, and the IPM reference book behind a spray decision.

TWO KINDS OF DATA LIVE BEHIND THIS MODULE AND THEY ARE NOT THE SAME KIND. An MRL
Record is a fact about a REGULATOR that an operation maintains, corrects and is
answerable for, so it is a doctype. `get_ipm_reference` reads
`erpnext_mcp/ipm_reference.py`, which is published literature — base
temperatures, IRAC groups, label intervals, what a product does to a lacewing —
and belongs to nobody's site. The first is written; the second is read and never
written, which is why there is no tool here that edits it.

`get_mrl_for_chemical_crop_market` IS THE ONE THAT MATTERS AND IT NEVER GUESSES.
It answers for one lane: this ingredient, this fruit, this destination. A miss
returns a miss — it will not fall back to another market's limit, will not
average, and will not offer the nearest crop. Every one of those would produce a
number that looks like an answer, and the question being asked is whether a load
can ship.

WHAT IT DOES INSTEAD OF GUESSING is say what it found nearby: the same
ingredient's limits in other markets, and the same market's limits on other
ingredients. That is research material for a person, clearly labelled as not
being the answer, rather than a substitute for one.

NOTHING HERE RESEARCHES. The farm_app called a model and wrote what came back;
that prompt is preserved verbatim as `prompt_templates.PROMPTS["mrl_research_single"]`
and is not wired to a writer. An MRL that arrived without somebody reading it is
an MRL nobody checked, and the whole value of the `source` column is that a human
put something in it.
"""

import frappe

from .. import compat, ipm_reference
from ..args import as_bool, as_choice, as_date, as_float, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.mrl_record.mrl_record import OFFICIAL_TIERS
from ..errors import ToolError
from ..result import ToolResult

MRL = "MRL Record"
REGISTER_CAP = 500

_MRL_FIELDS = (
	"name",
	"chemical",
	"crop",
	"market",
	"mrl_ppm",
	"company",
	"source",
	"source_tier",
	"confidence",
	"substance_status",
	"is_default_mrl",
	"crop_group_match",
	"effective_date",
	"expiry_date",
	"research_notes",
	"research_response",
	"notes",
)

#: Carried into every result that reports a limit. An MRL quoted without it is a
#: number somebody will ship on.
MRL_CAVEAT = (
	"An MRL on this site is what somebody recorded, not a live read of the regulator. "
	"Limits are revised, harmonised and withdrawn between seasons, and a stale one is more "
	"dangerous than a missing one because nobody goes looking. Check the source and the "
	"re-check date before a load moves."
)


#: The blank values. `0` is deliberately not among them — see `_same`.
_BLANK = (None, "")


def _same(before, after) -> bool:
	"""Whether staging `after` over `before` would change nothing.

	NOT `str(before or "") == str(after or "")`, which is the obvious spelling and
	SILENTLY DROPS ZERO: `0 or ""` is `""`, so setting a value of 0 on an empty
	column stages nothing, the write is lost, and the only symptom is a required
	field the caller believes they supplied.

	Zero is a real value in every numeric column these tools write — a non-detect
	residue limit, a cultural fit score of nothing, a battery reading of 0% — so
	blank and zero have to be told apart here rather than collapsed.
	"""
	if before in _BLANK and after in _BLANK:
		return True
	if before in _BLANK or after in _BLANK:
		return False
	return str(before) == str(after)


def _require(doctype: str) -> None:
	if not compat.doctype_exists(doctype):
		raise ToolError(f"{doctype} is not available on this site — run `bench migrate` to install it.")


def _date(value):
	return str(value) if value else None


def _describe_mrl(row: dict) -> dict:
	expiry = _date(row.get("expiry_date"))
	tier = str(row.get("source_tier") or "")
	return {
		"name": row.get("name"),
		"chemical": row.get("chemical"),
		"crop": row.get("crop"),
		"market": row.get("market"),
		"mrl_ppm": float(row.get("mrl_ppm") or 0),
		"company": row.get("company") or None,
		"source": row.get("source"),
		"source_tier": tier or None,
		# A figure read off a register and a figure inferred from a harmonisation
		# rule are both useful and are not interchangeable. Said plainly rather
		# than left as a digit a caller has to know how to read.
		"official_source": tier in OFFICIAL_TIERS,
		"confidence": row.get("confidence") or None,
		"substance_status": row.get("substance_status") or None,
		"is_default_mrl": compat.checked(row.get("is_default_mrl")),
		"crop_group_match": compat.checked(row.get("crop_group_match")),
		"effective_date": _date(row.get("effective_date")),
		"expiry_date": expiry,
		"needs_recheck": bool(expiry and expiry < frappe.utils.today()),
		"research_notes": row.get("research_notes") or None,
		"notes": row.get("notes") or None,
	}


def _mrl_warnings(described: dict) -> list:
	"""Everything about this figure that should reach somebody before a load moves."""
	warnings = []
	if described["needs_recheck"]:
		warnings.append(
			f"This limit was due for re-check on {described['expiry_date']}. It is being reported "
			"because it is what is on file, not because it is current."
		)
	if not described["official_source"]:
		warnings.append(
			f"Source tier {described['source_tier'] or 'unrecorded'} — this figure was "
			"cross-referenced or inferred rather than read off an official register. Usable for "
			"planning; not a basis for shipping without confirmation."
		)
	if described["is_default_mrl"]:
		warnings.append(
			"This is the market's blanket default rather than a limit set for this ingredient on "
			"this crop, which means nobody has set one. Planning a spray programme around a "
			"default is planning around the absence of a decision."
		)
	if described["crop_group_match"]:
		warnings.append(
			"Matched through a crop GROUP rather than this crop specifically. Legitimate, and one "
			"step further from certainty."
		)
	if described["substance_status"] in ("Banned", "Not Registered"):
		warnings.append(
			f"This substance is recorded as {described['substance_status']} in this market. A ban "
			"refuses the load regardless of the residue found — the limit below is not a "
			"permission."
		)
	if described["confidence"] == "Low":
		warnings.append("Recorded at low confidence by whoever found it.")
	return warnings


def _write_mrl(doc, args: dict, creating: bool) -> dict:
	changed = {}

	def stage(key, value):
		before = doc.get(key)
		if not _same(before, value):
			changed[key] = [before, value]
			doc.set(key, value)

	if creating or "chemical" in args:
		stage("chemical", as_str(args, "chemical", required=creating))
	for key, doctype in (("crop", "Crop"), ("market", "Market")):
		if creating or key in args:
			value = as_str(args, key, required=creating)
			if value and not frappe.db.exists(doctype, value):
				raise ToolError(
					f"{doctype} {value!r} does not exist on this site. An MRL filed against a "
					f"{doctype.lower()} nothing else knows about is one no shipping check will "
					"ever find."
				)
			stage(key, value)
	if creating or "mrl_ppm" in args:
		if args.get("mrl_ppm") is None and creating:
			raise ToolError("mrl_ppm is required.")
		if "mrl_ppm" in args:
			stage("mrl_ppm", as_float(args.get("mrl_ppm"), "mrl_ppm"))
	if creating or "source" in args:
		stage("source", as_str(args, "source", required=creating))
	for key in ("source_tier", "confidence", "substance_status"):
		if key in args:
			value = as_str(args, key)
			stage(key, as_choice(MRL, key, value, key) if value else "")
	for key in ("is_default_mrl", "crop_group_match"):
		if key in args:
			stage(key, 1 if as_bool(args, key) else 0)
	for key in ("effective_date", "expiry_date"):
		if key in args:
			stage(key, as_date(args, key) or "")
	for key in ("research_notes", "research_response", "notes"):
		if key in args:
			stage(key, as_str(args, key))
	return changed


def create_mrl_record(args: dict) -> ToolResult:
	"""Record one limit for one ingredient on one crop into one market."""
	_require(MRL)
	doc = frappe.new_doc(MRL)
	company = resolve_company(as_str(args, "company"))
	if company and as_str(args, "company"):
		doc.company = company
	_write_mrl(doc, args, creating=True)
	doc.insert(ignore_permissions=True)

	described = _describe_mrl(dict(doc.as_dict()))
	return ToolResult(
		data={
			**described,
			"warnings": _mrl_warnings(described),
			"caveat": MRL_CAVEAT,
		},
		summary=(
			f"{doc.name}: {described['chemical']} on {described['crop']} into "
			f"{described['market']} = {described['mrl_ppm']} ppm"
		),
		docstatus_delta="none → 0 (created)",
	)


def get_mrl_record(args: dict) -> ToolResult:
	"""One limit in full, with everything that qualifies it."""
	_require(MRL)
	name = as_str(args, "mrl_record", required=True)
	row = frappe.db.get_value(MRL, name, compat.existing_fields(MRL, _MRL_FIELDS), as_dict=True)
	if not row:
		raise ToolError(f"MRL Record {name!r} does not exist.")
	described = _describe_mrl(dict(row))
	return ToolResult(
		data={
			**described,
			"research_response": row.get("research_response") or None,
			"warnings": _mrl_warnings(described),
			"caveat": MRL_CAVEAT,
		},
		summary=(
			f"{described['chemical']} / {described['crop']} / {described['market']}: "
			f"{described['mrl_ppm']} ppm"
		),
	)


def list_mrl_records(args: dict) -> ToolResult:
	"""Limits on file, with the stale ones and the inferred ones named."""
	_require(MRL)
	limit = as_limit(args)

	filters = {}
	for key in ("chemical", "crop", "market"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	company = resolve_company(as_str(args, "company"))
	if company and as_str(args, "company"):
		filters["company"] = company
	for key in ("source_tier", "confidence", "substance_status"):
		value = as_str(args, key)
		if value:
			filters[key] = as_choice(MRL, key, value, key)

	rows = frappe.db.get_all(
		MRL,
		filters=filters,
		fields=compat.existing_fields(MRL, _MRL_FIELDS),
		order_by="chemical asc, crop asc, market asc",
		limit=min(limit, REGISTER_CAP),
	)
	records = [_describe_mrl(dict(row)) for row in rows]

	if as_bool(args, "needs_recheck") is True:
		records = [row for row in records if row["needs_recheck"]]

	by_market: dict = {}
	for row in records:
		by_market[row["market"]] = by_market.get(row["market"], 0) + 1

	return ToolResult(
		data={
			"record_count": len(records),
			"by_market": dict(sorted(by_market.items())),
			"needs_recheck": [row["name"] for row in records if row["needs_recheck"]],
			"inferred_sources": [row["name"] for row in records if not row["official_source"]],
			"default_limits": [row["name"] for row in records if row["is_default_mrl"]],
			"banned_substances": [
				row["name"] for row in records if row["substance_status"] in ("Banned", "Not Registered")
			],
			"caveat": MRL_CAVEAT,
			"records": records,
		},
		summary=f"{len(records)} MRL record(s)",
	)


def update_mrl_record(args: dict) -> ToolResult:
	"""Revise a limit — most often because the regulator did."""
	_require(MRL)
	name = as_str(args, "mrl_record", required=True)
	if not frappe.db.exists(MRL, name):
		raise ToolError(f"MRL Record {name!r} does not exist. Nothing was changed.")

	doc = frappe.get_doc(MRL, name)
	changed = _write_mrl(doc, args, creating=False)
	if not changed:
		raise ToolError(
			"nothing to change. Pass at least one of: chemical, crop, market, mrl_ppm, source, "
			"source_tier, confidence, substance_status, is_default_mrl, crop_group_match, "
			"effective_date, expiry_date, research_notes, research_response, notes."
		)
	doc.save(ignore_permissions=True)
	described = _describe_mrl(dict(doc.as_dict()))
	data = {**described, "changed": changed, "warnings": _mrl_warnings(described)}
	if "mrl_ppm" in changed and "source" not in changed:
		data["note"] = (
			"The limit moved and the source did not. A revised figure almost always comes from a "
			"revised regulation — if it did, record which, or the record now cites a document "
			"that says something else."
		)
	return ToolResult(
		data=data,
		summary=f"{doc.name}: {len(changed)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


# ── get_mrl_for_chemical_crop_market ────────────────────────────────────────
def get_mrl_for_chemical_crop_market(args: dict) -> ToolResult:
	"""The limit for one lane, or an honest miss with the neighbouring evidence."""
	_require(MRL)
	chemical = as_str(args, "chemical", required=True)
	crop = as_str(args, "crop", required=True)
	market = as_str(args, "market", required=True)

	filters = {"chemical": chemical, "crop": crop, "market": market}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company, required=True)

	rows = frappe.db.get_all(
		MRL,
		filters=filters,
		fields=compat.existing_fields(MRL, _MRL_FIELDS),
		order_by="modified desc",
		limit=2,
	)
	if rows:
		described = _describe_mrl(dict(rows[0]))
		return ToolResult(
			data={
				"found": True,
				"chemical": chemical,
				"crop": crop,
				"market": market,
				"mrl_ppm": described["mrl_ppm"],
				**described,
				"warnings": _mrl_warnings(described),
				"caveat": MRL_CAVEAT,
			},
			summary=f"{chemical} on {crop} into {market}: {described['mrl_ppm']} ppm",
		)

	# The miss. Everything below is research material and is labelled as such —
	# see the module docstring on why none of it is offered as a substitute.
	same_chemical = [
		_describe_mrl(dict(row))
		for row in frappe.db.get_all(
			MRL,
			filters={"chemical": chemical, "crop": crop},
			fields=compat.existing_fields(MRL, _MRL_FIELDS),
			order_by="market asc",
			limit=50,
		)
	]
	same_market = [
		_describe_mrl(dict(row))
		for row in frappe.db.get_all(
			MRL,
			filters={"market": market, "crop": crop},
			fields=compat.existing_fields(MRL, _MRL_FIELDS),
			order_by="chemical asc",
			limit=50,
		)
	]
	return ToolResult(
		data={
			"found": False,
			"chemical": chemical,
			"crop": crop,
			"market": market,
			"mrl_ppm": None,
			"why": (
				f"No limit is on file for {chemical} on {crop} into {market}. This tool does not "
				"fall back to another market's figure, average across markets, or offer the "
				"nearest crop — every one of those returns something that looks like an answer "
				"to a question about whether a load can ship."
			),
			"same_chemical_other_markets": same_chemical,
			"same_market_other_chemicals": same_market,
			"research_prompt": (
				"prompt_templates.PROMPTS['mrl_research_single'] holds the research prompt this "
				"app inherited, with the four-tier source ladder. What it returns still has to be "
				"read and recorded by a person with create_mrl_record."
			),
			"caveat": MRL_CAVEAT,
		},
		summary=f"no MRL on file for {chemical} on {crop} into {market}",
	)


# ── get_ipm_reference ───────────────────────────────────────────────────────
def get_ipm_reference(args: dict) -> ToolResult:
	"""The published IPM reference book: pest models, efficacy, toxicity, labels.

	READ-ONLY IN THE STRONGEST SENSE — it touches no doctype and no site data at
	all, so it works on a bench with nothing installed. What it returns is
	literature, not this farm's records, and it says so in every result.
	"""
	table = as_str(args, "table")
	pest = as_str(args, "pest")
	product = as_str(args, "product")
	crop = as_str(args, "crop")
	beneficial = as_str(args, "beneficial")

	data: dict = {
		"sources": list(ipm_reference.SOURCES),
		"caveat": ipm_reference.LABEL_CAVEAT,
		"is_site_data": False,
		"tables_available": list(ipm_reference.table_names()),
		"row_counts": ipm_reference.summary(),
	}

	asked = False
	if pest:
		asked = True
		data["pest_model"] = ipm_reference.pest_model(pest) or None
		data["damage_profile"] = ipm_reference.damage_profile(pest, crop)
		data["effective_products"] = ipm_reference.efficacy_against(pest)
		if not data["pest_model"] and not data["effective_products"]:
			data["pest_miss"] = (
				f"Nothing on file for {pest!r}. Matching here is exact apart from case and "
				"spacing — no fuzzy matching, because the near-misses in this vocabulary "
				"('Cherry Slug' and 'Pear Slug', 'Spider Mites') are the ones that must not be "
				"bridged automatically. Check the spelling against pest_models."
			)
	if product:
		asked = True
		data["product"] = ipm_reference.product(product) or None
		data["labels"] = ipm_reference.labels_for(product, crop)
		data["beneficial_toxicity"] = ipm_reference.toxicity_of(
			(data["product"] or {}).get("product") or product
		)
		if pest:
			data["rotation_partners"] = ipm_reference.rotation_partners(
				(data["product"] or {}).get("product") or product, pest
			)
	if beneficial:
		asked = True
		data["beneficial"] = ipm_reference.beneficial(beneficial) or None
		data["activity"] = [
			row
			for row in ipm_reference.BENEFICIAL_ACTIVITY
			if row["beneficial"].casefold() == beneficial.casefold()
			and (not crop or row["crop"].casefold() == crop.casefold())
		]
		data["harmed_by"] = sorted(
			(
				row
				for row in ipm_reference.BENEFICIAL_TOXICITY
				if row["beneficial"].casefold() == beneficial.casefold()
			),
			key=lambda row: float(row.get("toxicity_score") or 0),
			reverse=True,
		)
	if table:
		asked = True
		rows = ipm_reference.TABLES.get(table)
		if rows is None:
			raise ToolError(
				f"{table!r} is not a reference table. Known: {', '.join(ipm_reference.table_names())}."
			)
		data["table"] = table
		data["rows"] = list(rows)

	if not asked:
		# No filter is a legitimate call — it is how a caller finds out what the
		# book holds — so it answers with the index rather than dumping 405 rows.
		data["next_step"] = (
			"Pass `pest`, `product`, `beneficial` or `table` to read something. With no filter "
			"this returns only the index, because the whole book is several hundred rows and "
			"almost no question needs all of it."
		)

	return ToolResult(
		data=data,
		summary="IPM reference: "
		+ ", ".join(
			part
			for part in (
				f"pest={pest}" if pest else "",
				f"product={product}" if product else "",
				f"beneficial={beneficial}" if beneficial else "",
				f"table={table}" if table else "",
			)
			if part
		)
		or "IPM reference index",
	)
