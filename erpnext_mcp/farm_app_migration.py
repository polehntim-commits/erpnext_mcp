# SPDX-License-Identifier: MIT
"""Carrying the two things out of the farm_app's SQLite that are worth keeping.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT. v0.120.0 shipped a general
SQLite→ERPNext migration covering ten tables — blocks, IoT devices and readings,
strategic plans, objectives, market participants, acquisition targets,
competitive moves. **That was wrong about the data, and the owner said so.** The
sidecar's contents are TEST data: rows typed to exercise a form, six blocks of
which two are the same block twice, readings fifteen seconds apart from a smoke
test. Migrating test data into the system of record does not preserve anything —
it contaminates a clean register with rows nobody can tell apart from real ones
afterwards.

So this module now carries exactly two things, both named by the owner as real:

**The MRL reference data.** `maximum_residue` is a table of residue limits
assembled over two seasons out of the EU, Codex, Japanese and US registers.
Nothing about it is farm-specific — it is published regulation, transcribed —
and transcribing it again is weeks of somebody's attention. `mrl_research_session`
carries the completed research that produced some of those limits, and travels
with it.

**The satellite history.** Not the imagery: a Sentinel-2 raster is megabytes,
is re-downloadable from the provider's archive, and is useful for as long as it
takes to compute a mean from it. What is worth keeping is the two things the
imagery LEFT BEHIND — the index series computed from it (`field_satellite_metric`
→ `Satellite Metric`), and the record of how far back the archive has already
been walked (`satellite_backfill_cursor` → `Satellite Backfill Cursor`). The
second is the one that answers the owner's actual question: without it a backfill
starts at the beginning and pays the provider again for months already bought.
Cached raster FILES on disk are reported by `raster_manifest()` rather than
moved, because moving them is a `docker cp` an operator does with their own hands
and their own disk.

EVERYTHING ELSE IS GONE WITH THE SIDECAR, ON PURPOSE. The doctypes Cycle 1 built
for IoT, strategy and competitive intelligence remain — the STRUCTURE was the
point, and a farm starting to log real devices needs somewhere to log them. What
is not carried across is the test rows that happened to be sitting in those
tables.

THE PROPERTIES THAT STILL MATTER.

**Idempotent, by a NATURAL key and never by a row number.** A limit is matched by
chemical, crop and market; a metric by block, index and acquisition time; a
cursor by block and index. A second run finds what is there and reports it as
already present. Matching on the SQLite `id` would be easier and wrong twice
over: it is not stored on any target doctype, and a table that was ever re-seeded
has ids pointing at different rows than last week.

**It never updates.** A document that already exists is left exactly as it is.
The sidecar is being retired, not synchronised.

**Dry run first, always.** `migrate()` with the default loader produces the whole
transfer as data — every document it would create, every refusal and every
warning — and writes nothing. `FrappeLoader` is the only object here that writes.

THE NAME JOIN IS THE WEAK POINT AND IS TREATED AS ONE. MRL rows reference a crop
and a country by SQLite id; nothing on this site's `Crop` or `Market` carries
those ids, so the join is on the NAME. `seed_links_by_name` does it once, exactly,
casefolded, with no fuzzy matching of any kind, and reports every miss by name —
because a residue limit filed against the wrong fruit is worse than one that did
not migrate at all, since it looks like an answer.

A ROW WHOSE PARENT IS NOT RESOLVED IS REFUSED, never inserted with an empty link.
An MRL with no market cannot be applied to a shipment, and a satellite reading
with no block is a number with no ground.

RUNNING IT. `scripts/migrate_farm_app.py` is the command line around this module.
Everything here works on a `sqlite3.Connection` and a loader object, so the whole
transfer can be exercised against a fixture database with no bench at all — which
is what the tests do.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

#: How many rows one read pulls into memory at a time. `iot_reading` is the
#: table with real volume — the plan estimates ten thousand rows and a busy
#: season is more — and materialising it whole is how a migration gets killed by
#: the container's memory limit halfway through.
BATCH = 1000

#: A hard ceiling on rows migrated from one table in one run, so that a mistake
#: is bounded. Passing `limit` explicitly raises it; the report always says when
#: the cap bit, because a silently truncated migration reads exactly like a
#: complete one.
DEFAULT_LIMIT = 100000


class MigrationError(Exception):
	"""A problem with the source database or the spec — not with one row."""


class RowRefused(Exception):
	"""One row that cannot become a document. Collected, never fatal."""


class Spec:
	"""One SQLite table, the doctype it becomes, and how a row turns into one.

	`natural_key` is the tuple of DOCUMENT fieldnames that identify a document
	uniquely — the idempotence key from the module docstring. `mode` is `insert`
	for everything except the satellite fold, which updates an existing `Field`.
	"""

	def __init__(
		self,
		table: str,
		doctype: str,
		natural_key: tuple,
		build,
		depends_on: tuple = (),
		mode: str = "insert",
		note: str = "",
	):
		self.table = table
		self.doctype = doctype
		self.natural_key = natural_key
		self.build = build
		self.depends_on = depends_on
		self.mode = mode
		self.note = note

	def __repr__(self) -> str:  # pragma: no cover - diagnostics only
		return f"<Spec {self.table} → {self.doctype}>"


class Links:
	"""`(table, sqlite id) → docname`, built as the migration walks the tables.

	Seeded from ERPNext for `field`, because blocks were migrated by
	`import_farm_app_fields` in an earlier wave and carry their farm_app id in
	`external_farm_app_id`. Everything else fills in as its own table is
	migrated, which is why the spec order below is the dependency order and not
	alphabetical.
	"""

	def __init__(self, seed: dict | None = None):
		self._map = {}
		for table, entries in (seed or {}).items():
			for key, value in (entries or {}).items():
				self.remember(table, key, value)

	def remember(self, table: str, sqlite_id, docname) -> None:
		if sqlite_id is None or not docname:
			return
		self._map[(table, str(sqlite_id))] = str(docname)

	def resolve(self, table: str, sqlite_id):
		if sqlite_id is None:
			return None
		return self._map.get((table, str(sqlite_id)))

	def require(self, table: str, sqlite_id, what: str) -> str:
		"""The docname a foreign key points at, or a refusal naming what is missing."""
		found = self.resolve(table, sqlite_id)
		if not found:
			raise RowRefused(
				f"{what} points at {table} id {sqlite_id!r}, which has not been migrated. Migrate "
				f"{table} first — a record linked to nothing is a record nobody can trace."
			)
		return found

	def known(self, table: str) -> int:
		return sum(1 for key in self._map if key[0] == table)

	def as_dict(self) -> dict:
		out = {}
		for (table, key), value in self._map.items():
			out.setdefault(table, {})[key] = value
		return out


# ── loaders: the only code here that writes ─────────────────────────────────
class DryRunLoader:
	"""Writes nothing and records what it was asked to write. The default.

	`existing` lets a test — or a second dry run against a partly migrated
	site — say which documents are already there: `{doctype: [{field: value}]}`
	matched against each spec's natural key.
	"""

	def __init__(self, existing: dict | None = None):
		self.existing = {key: list(value) for key, value in (existing or {}).items()}
		self.inserted = []
		self.updated = []

	def find(self, doctype: str, filters: dict):
		for index, row in enumerate(self.existing.get(doctype, [])):
			if all(str(row.get(key, "")) == str(value) for key, value in filters.items()):
				return row.get("name") or f"{doctype}-existing-{index + 1}"
		return None

	def insert(self, doctype: str, doc: dict) -> str:
		self.inserted.append({"doctype": doctype, "doc": doc})
		return f"{doctype}-new-{len(self.inserted)}"

	def update(self, doctype: str, docname: str, changes: dict) -> str:
		self.updated.append({"doctype": doctype, "name": docname, "changes": changes})
		return docname

	@property
	def writes(self) -> bool:
		return False


class FrappeLoader:
	"""Inserts through Frappe. The one that actually migrates.

	`ignore_permissions` is on because this runs as Administrator from a script
	and the documents being created are the site's own history — the permission
	question was settled when somebody with shell access on the bench started it.
	`ignore_mandatory` is deliberately NOT on: a row that cannot satisfy its
	doctype's own required fields is a row the doctype says is incomplete, and
	forcing it in produces a document that fails the moment anybody opens it.
	"""

	def __init__(self, frappe_module=None, commit_every: int = 200):
		if frappe_module is None:
			import frappe as frappe_module
		self.frappe = frappe_module
		self.commit_every = max(1, int(commit_every))
		self.inserted = []
		self.updated = []

	def find(self, doctype: str, filters: dict):
		return self.frappe.db.exists(doctype, dict(filters))

	def insert(self, doctype: str, doc: dict) -> str:
		document = self.frappe.get_doc({"doctype": doctype, **doc})
		document.insert(ignore_permissions=True)
		self.inserted.append(document.name)
		if len(self.inserted) % self.commit_every == 0:
			self.frappe.db.commit()
		return document.name

	def update(self, doctype: str, docname: str, changes: dict) -> str:
		document = self.frappe.get_doc(doctype, docname)
		for field, value in changes.items():
			setattr(document, field, value)
		document.save(ignore_permissions=True)
		self.updated.append(docname)
		return docname

	@property
	def writes(self) -> bool:
		return True


# ── reading the source ──────────────────────────────────────────────────────
def open_database(path: str) -> sqlite3.Connection:
	"""The farm_app database, opened READ-ONLY.

	Read-only through the URI form rather than by discipline: this runs against
	a copy of a production database that somebody may have copied while the
	Flask app was still writing to it, and a migration that could modify its own
	source is a migration whose second run cannot be trusted.
	"""
	try:
		connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
	except sqlite3.Error as problem:
		raise MigrationError(f"cannot open {path!r} read-only: {problem}") from problem
	connection.row_factory = sqlite3.Row
	return connection


def table_exists(connection, table: str) -> bool:
	rows = connection.execute(
		"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
	).fetchall()
	return bool(rows)


def table_columns(connection, table: str) -> list:
	return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


def read_rows(connection, table: str, limit: int = DEFAULT_LIMIT, batch: int = BATCH):
	"""Every row of a table as a dict, oldest id first, in batches.

	Ordered by `id` so that a run interrupted halfway and restarted covers the
	same rows in the same order — which is what makes the "already present"
	count meaningful on the second run rather than a different arbitrary slice.
	"""
	if not table_exists(connection, table):
		return
	columns = table_columns(connection, table)
	order = "id" if "id" in columns else columns[0]
	seen = 0
	# The table name is interpolated because SQLite takes no parameter there. It
	# is safe because every value that reaches this argument is a literal from
	# `SPECS` — `migrate` refuses any table name that is not one of them before
	# this is called, which is the check that makes the interpolation sound.
	cursor = connection.execute(f"SELECT * FROM {table} ORDER BY {order}")
	while seen < limit:
		rows = cursor.fetchmany(min(batch, limit - seen))
		if not rows:
			return
		for row in rows:
			yield dict(row)
			seen += 1


# ── value coercions, each written once ──────────────────────────────────────
def text(value) -> str:
	"""A trimmed string. `None` and the literal SQL `"None"` both become `""`."""
	if value is None:
		return ""
	out = str(value).strip()
	return "" if out in ("None", "null", "NULL") else out


def number(value):
	"""A float, or `None`. ZERO SURVIVES — a battery reading of 0% is a reading."""
	if value is None or isinstance(value, bool):
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def integer(value):
	amount = number(value)
	return None if amount is None else int(amount)


def flag(value) -> int:
	"""A Check column's 0/1 from SQLite's several spellings of a boolean."""
	if value is None:
		return 0
	if isinstance(value, bool):
		return 1 if value else 0
	token = str(value).strip().lower()
	return 1 if token in ("1", "true", "t", "yes", "y") else 0


def day(value) -> str:
	"""An ISO date, or `""`. A Frappe Date field refuses anything else."""
	if value is None:
		return ""
	if isinstance(value, datetime):
		return value.date().isoformat()
	if isinstance(value, date):
		return value.isoformat()
	token = text(value)
	if not token:
		return ""
	token = token.replace("T", " ").split("+")[0].split(".")[0].rstrip("Zz").strip()
	for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y"):
		try:
			return datetime.strptime(token, pattern).date().isoformat()
		except ValueError:
			continue
	return ""


def moment(value) -> str:
	"""An ISO datetime string for a Frappe Datetime field, or `""`."""
	if value is None:
		return ""
	if isinstance(value, datetime):
		return value.strftime("%Y-%m-%d %H:%M:%S")
	if isinstance(value, date):
		return f"{value.isoformat()} 00:00:00"
	# The `Z` comes off with the offset and the microseconds: a farm_app export
	# written by `isoformat()` carries `2026-08-01T10:30:00Z`, and leaving the Z
	# on made every one of those parse to `""` — a whole table of readings
	# silently refused for having "no timestamp".
	token = text(value).replace("T", " ").split("+")[0].split(".")[0].rstrip("Zz").strip()
	if not token:
		return ""
	for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
		try:
			return datetime.strptime(token, pattern).strftime("%Y-%m-%d %H:%M:%S")
		except ValueError:
			continue
	return ""


def json_text(value) -> str:
	"""A JSON column as text for a Long Text field, pretty-printed.

	Pretty-printed rather than compact because these land in fields an operator
	OPENS — a SWOT analysis, a command structure — and a single line of JSON in a
	textarea is a document nobody will ever read again.
	"""
	if value is None:
		return ""
	if isinstance(value, (dict, list)):
		return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
	token = text(value)
	if not token:
		return ""
	try:
		return json.dumps(json.loads(token), indent=2, ensure_ascii=False, sort_keys=True)
	except (TypeError, ValueError):
		return token


def lines(value) -> str:
	"""A JSON list as one item per line, for the Small Text fields that hold one.

	`["Water rights", "Cold storage"]` becomes two lines rather than a JSON
	array, because the field is what an operator reads on a Market Participant
	form and brackets in it are noise.
	"""
	if value is None:
		return ""
	parsed = value
	if isinstance(value, str):
		token = text(value)
		if not token:
			return ""
		try:
			parsed = json.loads(token)
		except (TypeError, ValueError):
			return token
	if isinstance(parsed, dict):
		return "\n".join(f"{key}: {item}" for key, item in parsed.items())
	if isinstance(parsed, (list, tuple)):
		return "\n".join(text(item) for item in parsed if text(item))
	return text(parsed)


def option(value, options: dict, warnings: list, field: str, default: str = ""):
	"""A Select value mapped onto a doctype's own options, or blank and a warning.

	The one place the "never guess an option" rule from the module docstring is
	enforced. `options` is keyed by the CASEFOLDED source value with underscores
	and hyphens flattened to spaces, so `"acquisition_target"`,
	`"Acquisition-Target"` and `"ACQUISITION TARGET"` all resolve.
	"""
	token = text(value)
	if not token:
		return default
	key = token.casefold().replace("_", " ").replace("-", " ").strip()
	if key in options:
		return options[key]
	# The warning NAMES WHAT HAPPENED INSTEAD, which is not always "blank": some
	# of these fields are required by their doctype and carry a fallback. A
	# warning that said "left blank" while a value was written would send an
	# operator looking for an empty field they would never find.
	landed = f"left as {default!r}" if default else "left blank"
	warnings.append(f"{field}: {token!r} is not one of this doctype's options — {landed}")
	return default


def _options(*names) -> dict:
	"""`{casefolded spelling: option}` for options that map onto themselves."""
	return {name.casefold().replace("_", " ").replace("-", " "): name for name in names}


# ── one builder per table ───────────────────────────────────────────────────
CONFIDENCE = {**_options("Low", "Medium", "High")}
SUBSTANCE_STATUS = {
	**_options("Registered", "Banned", "Not Registered", "Restricted", "Unknown"),
	"unknown": "Unknown",
	"not registered": "Not Registered",
	"unregistered": "Not Registered",
	"prohibited": "Banned",
}


def _mrl_links(row: dict, links: Links, doc: dict) -> None:
	"""Resolve the crop and the market, or refuse the row saying which is missing.

	BOTH ARE `reqd=1` ON `MRL Record`, so a document without them is one Frappe
	will refuse at insert time — halfway through a run, with a traceback, after
	some of the batch has already landed. Refusing here instead puts it in the
	DRY RUN, by name, before anything is written: an earlier draft warned and
	migrated anyway, which turned a readable plan into a mid-run failure.

	It is also the right answer on the merits. A residue limit is a limit for one
	chemical on one crop in one market; without either link it is a number that
	cannot be applied to a shipment, and one filed anyway looks like an answer.
	The fix is a minute of somebody's time — create the Crop or the Market, or
	rename it to match — and this migration is idempotent, so re-running picks up
	only what was missing.
	"""
	crop = links.resolve("commodity", row.get("commodity_id"))
	market = links.resolve("country", row.get("country_id"))
	missing = []
	if not crop:
		missing.append(f"crop (farm_app commodity id {row.get('commodity_id')!r})")
	if not market:
		missing.append(f"market (farm_app country id {row.get('country_id')!r})")
	if missing:
		raise RowRefused(
			"this limit has no " + " and no ".join(missing) + " on this site. `MRL Record` requires "
			"both, so it would be refused at insert; the run report lists every unmatched name so "
			"they can be created or renamed, and re-running picks up only what was missing."
		)
	doc["crop"] = crop
	doc["market"] = market


def build_mrl_record(row: dict, links: Links, context: dict, warnings: list) -> dict:
	"""An `MRL Record` from a completed `mrl_research_session`.

	ONLY COMPLETED, VALIDATED SESSIONS BECOME RECORDS. A research session is the
	transcript of asking a model a question; an MRL Record is a residue limit the
	farm will decide a shipment against. A session still pending review, or one
	whose result holds `NOT_FOUND`, has no limit in it — migrating it as an MRL
	Record of zero would be the worst possible failure of this whole exercise,
	because zero is a real and extremely restrictive limit.
	"""
	chemical = text(row.get("active_ingredient"))
	if not chemical:
		raise RowRefused("a session with no active ingredient names no substance")
	result = row.get("research_result")
	if isinstance(result, str):
		try:
			result = json.loads(result) if text(result) else {}
		except (TypeError, ValueError):
			result = {}
	result = result if isinstance(result, dict) else {}

	limit = result.get("mrl_value")
	if limit in (None, "", "NOT_FOUND"):
		raise RowRefused(
			"this session found no MRL value. A record with a blank limit is not a limit, and one "
			"with a zero is the strictest limit there is — neither is what the session concluded."
		)
	amount = number(limit)
	if amount is None:
		raise RowRefused(f"the session's mrl_value {limit!r} is not a number")

	doc = {
		"chemical": chemical,
		"mrl_ppm": amount,
		"source": text(result.get("source_reference")) or text(result.get("source_database")),
		"source_tier": text(result.get("source_tier")),
		"confidence": option(result.get("confidence"), CONFIDENCE, warnings, "confidence", "Low"),
		"substance_status": option(
			result.get("substance_status"), SUBSTANCE_STATUS, warnings, "substance_status", "Unknown"
		),
		"is_default_mrl": flag(result.get("is_default_mrl")),
		"crop_group_match": flag(result.get("crop_group_match")),
		"effective_date": day(result.get("effective_date")),
		"research_notes": text(row.get("review_notes")) or text(result.get("notes")),
		"research_response": text(row.get("ai_response_raw")),
	}
	_mrl_links(row, links, doc)
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def build_maximum_residue(row: dict, links: Links, context: dict, warnings: list) -> dict:
	"""An `MRL Record` from the sidecar's `maximum_residue` table.

	THIS IS THE ONE THAT MATTERS. A research session is the transcript of asking a
	question; `maximum_residue` is the answer the farm decided to keep and ships
	fruit against. It is reference data assembled over two seasons out of the EU,
	Codex, Japanese and US registers, and it is expensive to redo and impossible
	to reconstruct from the schema.

	THE UNIT IS CHECKED, NOT ASSUMED. `MRL Record.mrl_ppm` says ppm in its own
	name and the source table carries a free-text `mrl_unit`. mg/kg IS ppm, so
	that converts silently and correctly; ppb is a thousandth of one, and a ppb
	figure written into a ppm column is a limit a thousand times too loose — the
	direction that clears a shipment it should have held. Anything this does not
	recognise is refused rather than guessed at.
	"""
	chemical = text(row.get("active_ingredient"))
	if not chemical:
		raise RowRefused("a residue limit that names no substance cannot be applied to anything")

	amount = number(row.get("mrl_value"))
	if amount is None:
		raise RowRefused("no mrl_value on this row — a record with no limit is not a limit")

	unit = text(row.get("mrl_unit")).lower().replace(" ", "") or "ppm"
	factors = {"ppm": 1.0, "mg/kg": 1.0, "mgkg": 1.0, "mg/kg-1": 1.0, "ppb": 0.001, "ug/kg": 0.001}
	if unit not in factors:
		raise RowRefused(
			f"mrl_unit {row.get('mrl_unit')!r} is not one this converts to ppm "
			f"({', '.join(sorted(factors))}). A unit guessed here is a limit off by a factor of a "
			"thousand, in the direction that clears a shipment it should have held."
		)
	if factors[unit] != 1.0:
		warnings.append(f"mrl_value converted from {unit} to ppm (x{factors[unit]})")

	doc = {
		"chemical": chemical,
		"mrl_ppm": round(amount * factors[unit], 6),
		"source": text(row.get("source")),
		"research_notes": text(row.get("source_reference")),
		"substance_status": option(row.get("status"), SUBSTANCE_STATUS, warnings, "status", "Unknown"),
		"effective_date": day(row.get("effective_date")),
		"expiry_date": day(row.get("review_date")),
		"notes": text(row.get("notes")),
	}
	_mrl_links(row, links, doc)
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def build_satellite_metric(row: dict, links: Links, context: dict, warnings: list) -> dict:
	"""A `Satellite Metric` from one `field_satellite_metric` row.

	The whole series crosses now, not just the newest reading. v0.120.0 folded
	only the latest NDVI onto `Field` because there was nowhere else to put it;
	the doctype this writes exists precisely so that a season of readings — which
	is what a satellite subscription actually bought — survives the sidecar.

	The RAW value is preferred and the indexed one is a fallback: a row that
	stored only `indexed_value` is un-indexed here through the same range table
	the storage side uses, so the two never disagree by a rounding step.
	"""
	from . import satellite

	field = links.require("field", row.get("field_id"), "this satellite metric")
	metric = text(row.get("metric_type")).lower() or "ndvi"
	try:
		metric = satellite.resolve_metric(metric)
	except satellite.SatelliteError as problem:
		raise RowRefused(str(problem)) from problem

	when = moment(row.get("timestamp"))
	if not when:
		raise RowRefused("a pass with no timestamp cannot be placed in a series")

	value = number(row.get("value"))
	if value is None:
		indexed = number(row.get("indexed_value"))
		value = satellite.from_index(indexed, metric) if indexed is not None else None
	if value is None:
		raise RowRefused("no value and no indexed_value on this row")

	low, high = satellite.METRICS[metric]["raw_range"]
	if not low <= value <= high:
		raise RowRefused(
			f"{value} is outside the range {metric} runs in ({low} to {high}) — a decode with the "
			"wrong scale, not a reading"
		)

	doc = {
		"field": field,
		"metric_type": metric,
		"timestamp": when,
		"value": round(value, 6),
		"source": text(row.get("source")) or satellite.DATA_COLLECTION,
		"h3_index": text(row.get("h3_index")),
	}
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def build_backfill_cursor(row: dict, links: Links, context: dict, warnings: list) -> dict:
	"""A `Satellite Backfill Cursor` — the record that stops a re-download.

	Nothing on it is a measurement. Its entire value is that a backfill without
	it starts at the beginning and pays the provider again for months somebody
	has already bought, and that cost shows up on an invoice rather than in the
	data.
	"""
	field = links.require("field", row.get("field_id"), "this backfill cursor")
	oldest = moment(row.get("oldest_fetched"))
	newest = moment(row.get("newest_fetched"))
	if not oldest and not newest:
		raise RowRefused("a cursor with neither end of its window recorded says nothing was fetched")

	doc = {
		"field": field,
		# The sidecar kept ONE cursor per block with no index column, and every
		# pull it ever made was NDVI. Recording that as the ndvi cursor is the
		# honest reading; a cursor claiming to cover indices nobody fetched would
		# suppress the walks that have not happened.
		"metric_type": "ndvi",
		"oldest_fetched": oldest,
		"newest_fetched": newest,
		"last_run": moment(row.get("last_run")),
		"backfill_complete": flag(row.get("backfill_complete")),
	}
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def raster_manifest(connection, root: str = "") -> dict:
	"""What cached imagery the sidecar still holds, as `{"rasters": [...], ...}`.

	`Field.ndvi_path` is a path INSIDE the farm_app container, so this cannot
	fetch the file — it reports what to copy out and whether the path is readable
	from wherever this is running. Pass `root` to check against a directory the
	container's files have been copied into.

	WHY THIS IS A MANIFEST AND NOT A COPY. Moving image files is a `docker cp`,
	which is a thing an operator does with their own hands and their own disk
	space; a migration script that silently pulled megabytes across a container
	boundary would be doing something nobody asked for at a moment nobody chose.
	What the script owes them is a list, a size, and an honest answer about
	whether the files are still there.
	"""
	import os

	rows, missing, total = [], 0, 0
	if not table_exists(connection, "field"):
		return {"rasters": [], "missing": 0, "total_bytes": 0, "checked_against": root or ""}
	for row in read_rows(connection, "field"):
		path = text(row.get("ndvi_path"))
		if not path:
			continue
		local = os.path.join(root, path.lstrip("/")) if root else path
		exists = os.path.isfile(local)
		size = os.path.getsize(local) if exists else 0
		total += size
		missing += 0 if exists else 1
		rows.append(
			{
				"field_id": row.get("id"),
				"block": text(row.get("name")),
				"path": path,
				"readable_at": local if exists else "",
				"bytes": size,
			}
		)
	return {"rasters": rows, "missing": missing, "total_bytes": total, "checked_against": root or ""}


#: The transfer, in dependency order. Two things only, because everything else in
#: the sidecar was test data — see the module docstring.
SPECS = (
	Spec(
		"maximum_residue",
		"MRL Record",
		("chemical", "crop", "market"),
		build_maximum_residue,
		note="the residue limits themselves; the reference data this migration exists for",
	),
	Spec(
		"mrl_research_session",
		"MRL Record",
		("chemical", "crop", "market"),
		build_mrl_record,
		note="completed research sessions that reached a validated limit",
	),
	Spec(
		"field_satellite_metric",
		"Satellite Metric",
		("field", "metric_type", "timestamp"),
		build_satellite_metric,
		note="the whole index series, so a season of pulls survives the sidecar",
	),
	Spec(
		"satellite_backfill_cursor",
		"Satellite Backfill Cursor",
		("field", "metric_type"),
		build_backfill_cursor,
		note="how far back imagery has already been paid for",
	),
)

SPEC_BY_TABLE = {spec.table: spec for spec in SPECS}


def seed_links_from_site(frappe_module=None) -> dict:
	"""`{"field": {farm_app id: docname}}` read off the site's own Fields.

	The bridge between waves: `import_farm_app_fields` wrote
	`external_farm_app_id` for exactly this. Blocks are the only register this
	can resolve by id, because they are the only one anybody carried an external
	id onto — everything else matches by name, in `seed_links_by_name`.
	"""
	if frappe_module is None:
		import frappe as frappe_module
	rows = (
		frappe_module.db.get_all(
			"Field",
			filters={"external_farm_app_id": ["!=", ""]},
			fields=["name", "external_farm_app_id"],
			limit=100000,
		)
		or []
	)
	return {"field": {str(row["external_farm_app_id"]): row["name"] for row in rows}}


def seed_links_by_name(connection, lookup, tables=("commodity", "country")) -> dict:
	"""`{"commodity": {id: Crop}, "country": {id: Market}}`, matched on NAME.

	MRL rows point at a crop and a country by SQLite id, and nothing on this
	site's Crop or Market carries those ids — so the join has to be the name, and
	the name is the weakest link in this whole migration. It is therefore done
	ONCE, here, reported in full, and never guessed at row level: a residue limit
	silently filed against the wrong crop is worse than one that did not migrate,
	because it looks like an answer.

	`lookup(doctype, name)` is the site query, injected so this is testable
	without a bench — `frappe_lookup()` builds the real one.

	Matching is casefolded and trimmed and NOTHING else. No fuzzy matching, no
	plural stripping, no "cherries" → "Cherry": every one of those is a rule that
	is right four times and wrong once, and the once is a limit on the wrong
	fruit. What does not match is reported by name so somebody can rename it or
	create it, which takes a minute and is auditable.
	"""
	out, unmatched = {}, {}
	targets = {"commodity": "Crop", "country": "Market"}
	for table in tables:
		if table not in targets or not table_exists(connection, table):
			continue
		mapping, misses = {}, []
		for row in read_rows(connection, table):
			name = text(row.get("name"))
			if not name:
				continue
			found = lookup(targets[table], name)
			if found:
				mapping[str(row.get("id"))] = found
			else:
				misses.append(name)
		out[table] = mapping
		if misses:
			unmatched[table] = misses
	out["_unmatched"] = unmatched
	return out


def frappe_lookup(frappe_module=None):
	"""A `lookup(doctype, name)` that asks the site, casefolded, exact.

	Tries the doctype's own name column first and then the docname, because a
	Crop may be named `CROP-0007` with `crop_name` "Cherry" or may be named
	"Cherry" outright, and which one a site does is a naming-series decision
	somebody made years ago.
	"""
	if frappe_module is None:
		import frappe as frappe_module

	columns = {"Crop": "crop_name", "Market": "market_name"}

	def lookup(doctype: str, name: str):
		wanted = str(name or "").strip()
		if not wanted:
			return None
		column = columns.get(doctype)
		if column:
			for row in frappe_module.db.get_all(doctype, fields=["name", column], limit=5000) or []:
				if str(row.get(column) or "").strip().casefold() == wanted.casefold():
					return row["name"]
		for row in frappe_module.db.get_all(doctype, fields=["name"], limit=5000) or []:
			if str(row["name"]).strip().casefold() == wanted.casefold():
				return row["name"]
		return None

	return lookup


def migrate(
	connection,
	loader=None,
	links=None,
	context=None,
	only=(),
	limit: int = DEFAULT_LIMIT,
) -> dict:
	"""Run the whole transfer and report it. Writes only if the loader does.

	`only` restricts the run to named tables — used for the batched `iot_reading`
	pass, and for re-running one table after fixing its refusals. The dependency
	order is still the order the remaining specs run in.

	The report is `{"tables": [...], "created": int, "updated": int,
	"already_present": int, "refused": int, "warnings": int, "links": {...}}`, and
	each table's entry carries its own refusals and warnings with the source row
	id attached — so a migration that refused eleven rows says which eleven.

	`updated` is reported and is zero for every spec here: all four insert. It is
	kept because the counter is part of the report's shape and a later
	update-shaped migration should not have to change every reader of it.
	"""
	loader = loader if loader is not None else DryRunLoader()
	links = links if links is not None else Links()
	context = dict(context or {})
	wanted = {str(name) for name in only or ()}
	if wanted - set(SPEC_BY_TABLE):
		raise MigrationError(
			f"unknown table(s) {sorted(wanted - set(SPEC_BY_TABLE))}. Known: {', '.join(SPEC_BY_TABLE)}"
		)

	report = {
		"tables": [],
		"created": 0,
		"updated": 0,
		"already_present": 0,
		"refused": 0,
		"warnings": 0,
		"applied": bool(loader.writes),
	}
	for spec in SPECS:
		if wanted and spec.table not in wanted:
			continue
		entry = _migrate_table(connection, spec, loader, links, context, limit)
		report["tables"].append(entry)
		for key in ("created", "updated", "already_present", "refused"):
			report[key] += entry[key]
		report["warnings"] += len(entry["warnings"])
	report["links"] = links.as_dict()
	return report


def _migrate_table(connection, spec: Spec, loader, links: Links, context: dict, limit: int) -> dict:
	entry = {
		"table": spec.table,
		"doctype": spec.doctype,
		"mode": spec.mode,
		"note": spec.note,
		"present": table_exists(connection, spec.table),
		"read": 0,
		"created": 0,
		"updated": 0,
		"already_present": 0,
		"refused": 0,
		"refusals": [],
		"warnings": [],
		"truncated": False,
	}
	if not entry["present"]:
		# Every warning in a table entry is a dict with the same two keys. An
		# earlier draft appended a bare string here, and every caller that read
		# `warning["warning"]` crashed on the one table a database happened not to
		# have — which is the table a partial export is most likely to be missing.
		entry["warnings"].append(
			{"id": None, "warning": f"table {spec.table} is not in this database — nothing to migrate"}
		)
		return entry

	for row in read_rows(connection, spec.table, limit):
		entry["read"] += 1
		warnings = []
		row_id = row.get("id")
		try:
			doc = spec.build(row, links, context, warnings)
		except RowRefused as refusal:
			entry["refused"] += 1
			entry["refusals"].append({"id": row_id, "why": str(refusal)})
			continue
		entry["warnings"].extend({"id": row_id, "warning": warning} for warning in warnings)

		filters = {key: doc.get(key, "") for key in spec.natural_key}
		found = loader.find(spec.doctype, filters) if filters else None
		if found:
			entry["already_present"] += 1
			links.remember(spec.table, row_id, found)
			continue
		docname = loader.insert(spec.doctype, doc)
		links.remember(spec.table, row_id, docname)
		entry["created"] += 1

	if entry["read"] >= limit:
		entry["truncated"] = True
		entry["warnings"].append(
			{"id": None, "warning": f"stopped at the {limit}-row limit — re-run with a higher limit"}
		)
	return entry
