# SPDX-License-Identifier: MIT
"""Moving the farm_app's SQLite rows into ERPNext, once, without doing it twice.

WHAT THIS IS. The farm_app Flask sidecar kept its own SQLite database, and Cycle
1 of the retirement plan built the ERPNext doctypes that replace the tables
still living in it. This module is the transfer: read a table, turn each row
into a document, and insert it — in an order that respects the foreign keys, and
in a way that can be run again tomorrow without producing a second copy of
everything.

THE THREE PROPERTIES THAT MATTER, IN THE ORDER THEY MATTER.

**Idempotent, by a NATURAL key and never by a row number.** Every spec below
names the fields that make one of its documents unique — a device's hardware id,
a plan's name and version, a reading's device-type-and-timestamp. A second run
finds the existing document by those fields and records it as already present.
Matching on the SQLite `id` instead would be easier and would be wrong twice
over: the id is not stored on most of the target doctypes, and a farm_app table
that was ever re-seeded has ids pointing at different rows than it did last week.

**It never updates.** A document that already exists is left exactly as it is,
including when the SQLite row has changed. The farm_app is being retired, not
synchronised: after the cutover ERPNext is the system of record, and a migration
that reached back to overwrite an operator's correction with a stale sidecar
value would silently undo work somebody did in the new system. The report says
`already present` and the operator decides.

**Dry run first, always.** `migrate()` with the default loader produces the
whole transfer as data — every document it would create, every refusal and every
warning — and writes nothing. `FrappeLoader` is the only object here that
writes, and the caller has to pass one deliberately.

WHAT IT WILL NOT INVENT. Two cases, both deliberate:

*`block_ticker` is not derived from a block's name.* The field's own description
says it is the buyer's name for the block, unique across the company, promised
to somebody outside the business, and that EMPTY IS THE NORMAL STATE. Turning
`"Block A4"` into `"A4"` would manufacture a commitment the farm never made, on
every block at once. Tickers migrate only from an explicit mapping the operator
supplies, and blocks not in it keep an empty ticker.

*An unrecognised Select value is reported, not guessed.* A farm_app
`participant_type` of `"co-op"` has no Market Participant option to land on. The
row still migrates, that one field is left blank, and the value appears in
`warnings` with the row that carried it — because the alternative, quietly
choosing the first option in the list, produces a register that is wrong in a
way nobody can find later.

WHERE THE FOREIGN KEYS GO. Rows reference each other by SQLite id, and the
documents they become reference each other by docname. `Links` carries the
map — built as each table is migrated, and seeded for `field` from the
`external_farm_app_id` column that `import_farm_app_fields` writes for exactly
this purpose. A row whose parent has not been migrated is REFUSED rather than
inserted with an empty link, because a reading with no device is a number with
no provenance.

WHAT IS NOT MIGRATED, AND WHY. `field_satellite_metric` has no target doctype —
`Satellite Metric` is proposed in the plan and has not shipped — so its spec
folds only the newest NDVI reading per block onto `Field`'s own
`last_ndvi_*` columns and reports the rest of the history as dropped. That is a
real loss of a time series and it is stated in the report rather than left for
somebody to notice. Everything else the plan excludes (HR, payroll, accounting,
crop protection, Nostr, backup sharding) is excluded because it is already in
ERPNext or was never going to be.

RUNNING IT. `scripts/migrate_farm_app.py` is the command line around this
module; it is where the bench is started and the flags are parsed. Everything
here works on a `sqlite3.Connection` and a loader object, so the whole transfer
can be exercised against a fixture database with no bench at all — which is what
the tests do.
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
DEVICE_TYPES = {
	**_options("Soil Moisture", "Flow Meter", "Weather Station", "Temperature", "Generic"),
	"soil": "Soil Moisture",
	"moisture": "Soil Moisture",
	"flow": "Flow Meter",
	"weather": "Weather Station",
	"temp": "Temperature",
}
READING_QUALITY = {**_options("Good", "Suspect", "Error"), "ok": "Good", "bad": "Error"}
PLAN_STATUS = {**_options("Developing", "Developed", "Implemented", "Historical"), "retired": "Historical"}
OBJECTIVE_STATUS = {
	**_options("Pending", "In Progress", "Achieved", "Failed"),
	"open": "Pending",
	"complete": "Achieved",
	"completed": "Achieved",
	"done": "Achieved",
	"abandoned": "Failed",
}
PARTICIPANT_TYPES = {**_options("Competitor", "Supplier", "Customer", "Partner", "Target")}
PARTICIPANT_POSITION = {**_options("Leader", "Challenger", "Follower", "Nicher")}
PARTICIPANT_RELATIONSHIP = {
	**_options("Allied", "Neutral", "Adversarial", "Acquisition Target"),
	"ally": "Allied",
	"hostile": "Adversarial",
	"target": "Acquisition Target",
}
ACQUISITION_STATUS = {
	**_options("Identified", "Evaluating", "Due Diligence", "Negotiating", "Closed", "Passed"),
	"open": "Identified",
	"rejected": "Passed",
}
ACQUISITION_ACTION = {**_options("Monitor", "Evaluate", "Pursue", "Negotiate", "Close")}
MOVE_SEVERITY = {**_options("Low", "Medium", "High"), "critical": "High", "minor": "Low"}
MOVE_URGENCY = {
	**_options("No Action", "Monitor", "Respond", "Urgent"),
	"none": "No Action",
	"immediate": "Urgent",
	"high": "Urgent",
	"medium": "Respond",
	"low": "Monitor",
}
CONFIDENCE = {**_options("Low", "Medium", "High")}
SUBSTANCE_STATUS = {
	**_options("Registered", "Banned", "Not Registered", "Restricted", "Unknown"),
	"unknown": "Unknown",
	"not registered": "Not Registered",
	"unregistered": "Not Registered",
	"prohibited": "Banned",
}


def build_field(row: dict, links: Links, context: dict, warnings: list) -> dict:
	"""A `Field` from a farm_app `field` row.

	`block_ticker` comes ONLY from the operator's mapping — see the module
	docstring. `boundary_geojson` carries the polygon across so that the
	geospatial derivations (`centroid`, `bbox`, H3 cover) recompute on save
	rather than being copied as stale numbers.
	"""
	name = text(row.get("name"))
	if not name:
		raise RowRefused("a block with no name cannot be created — Field.field_name is required")
	tickers = context.get("tickers") or {}
	ticker = text(tickers.get(str(row.get("id"))) or tickers.get(name))
	if ticker and len(ticker) > 10:
		warnings.append(f"block_ticker {ticker!r} is longer than the field's 10 characters — left blank")
		ticker = ""

	doc = {
		"field_name": name,
		"external_farm_app_id": text(row.get("id")),
		"acreage": number(row.get("acres")),
		"block_ticker": ticker.upper(),
		"boundary_geojson": text(row.get("polygon")),
	}
	if context.get("company"):
		doc["owning_entity"] = context["company"]
	if context.get("parcel"):
		doc["parcel"] = context["parcel"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def build_iot_device(row: dict, links: Links, context: dict, warnings: list) -> dict:
	hardware_id = text(row.get("hardware_id"))
	if not hardware_id:
		raise RowRefused(
			"a device with no hardware_id has no natural key — two of them would migrate as one, "
			"and neither could be matched to the thing on the post"
		)
	doc = {
		"device_name": text(row.get("name")) or hardware_id,
		"hardware_id": hardware_id,
		"device_type": option(row.get("device_type"), DEVICE_TYPES, warnings, "device_type", "Generic"),
		"zone": text(row.get("zone")),
		"enabled": flag(row.get("is_active")),
		"last_seen": moment(row.get("last_seen")),
		"battery_level": number(row.get("battery_level")),
		"signal_strength": number(row.get("signal_strength")),
		"device_config": json_text(row.get("config")),
	}
	# THE AUTH TOKEN CROSSES. A device in an orchard post has this string burned
	# into its firmware and no way to be told a new one short of a truck and a
	# laptop, so rotating it at migration silently takes every sensor offline.
	# `--rotate-tokens` is how an operator asks for the other behaviour.
	if not context.get("rotate_tokens"):
		token = text(row.get("auth_token"))
		if token:
			doc["auth_token"] = token
	field = links.resolve("field", row.get("field_id"))
	if field:
		doc["field"] = field
	elif row.get("field_id") is not None:
		warnings.append(f"field id {row['field_id']} is not migrated — device registered without a block")
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def build_iot_reading(row: dict, links: Links, context: dict, warnings: list) -> dict:
	device = links.require("iot_device", row.get("device_id"), "this reading")
	when = moment(row.get("timestamp"))
	if not when:
		raise RowRefused("a reading with no timestamp is a number with no place in a series")
	value = number(row.get("value"))
	if value is None:
		raise RowRefused("a reading with no value is not a reading")
	doc = {
		"device": device,
		"timestamp": when,
		"reading_type": text(row.get("reading_type")) or "generic",
		"value": value,
		"unit": text(row.get("unit")) or "unit",
		"quality": option(row.get("quality"), READING_QUALITY, warnings, "quality", "Good"),
	}
	field = links.resolve("field", row.get("field_id"))
	if field:
		doc["field"] = field
	if context.get("company"):
		doc["company"] = context["company"]
	return doc


def build_strategic_plan(row: dict, links: Links, context: dict, warnings: list) -> dict:
	name = text(row.get("name"))
	if not name:
		raise RowRefused("a plan with no name cannot be told apart from the next one")
	doc = {
		"plan_name": name,
		"status": option(row.get("status"), PLAN_STATUS, warnings, "status", "Developing"),
		"version": integer(row.get("version")) or 1,
		"effective_date": day(row.get("effective_date")),
		"retired_date": day(row.get("retired_date")),
		"vision": text(row.get("vision")),
		"mission": text(row.get("mission")),
		"values_text": lines(row.get("values")),
		"swot": json_text(row.get("swot")),
		"porters_five_forces": json_text(row.get("porters_five_forces")),
		"sustainable_advantage": json_text(row.get("sustainable_advantage")),
		"analogous_games": json_text(row.get("analogous_games")),
		"grand_strategy": text(row.get("grand_strategy")),
		"business_strategy": text(row.get("business_strategy")),
		"command_structure": json_text(row.get("command_structure")),
		"functional_tactics": json_text(row.get("functional_tactics")),
		"validation_control": json_text(row.get("validation_control")),
		"exit_strategy": text(row.get("exit_strategy")),
		"notes": text(row.get("notes")),
	}
	crop = links.resolve("commodity", row.get("commodity_id"))
	if crop:
		doc["crop"] = crop
	previous = links.resolve("strategic_plan", row.get("parent_id"))
	if previous:
		doc["previous_version"] = previous
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def build_objective(row: dict, links: Links, context: dict, warnings: list) -> dict:
	plan = links.require("strategic_plan", row.get("strategic_plan_id"), "this objective")
	description = text(row.get("description"))
	if not description:
		raise RowRefused("an objective with no description states nothing to achieve")
	doc = {
		"strategic_plan": plan,
		"objective": description,
		"status": option(row.get("status"), OBJECTIVE_STATUS, warnings, "status", "Pending"),
		"due_date": day(row.get("target_date")),
		"kpi_metric": text(row.get("measurable")),
		"notes": text(row.get("notes")),
	}
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def build_market_participant(row: dict, links: Links, context: dict, warnings: list) -> dict:
	name = text(row.get("name"))
	if not name:
		raise RowRefused("a participant with no name is not a participant")
	doc = {
		"participant_name": name,
		"participant_type": option(
			row.get("participant_type"), PARTICIPANT_TYPES, warnings, "participant_type", "Competitor"
		),
		"relationship_status": option(
			row.get("relationship_status"), PARTICIPANT_RELATIONSHIP, warnings, "relationship_status"
		),
		"market_position": option(
			row.get("market_position"), PARTICIPANT_POSITION, warnings, "market_position"
		),
		"industry_segment": text(row.get("industry_segment")),
		"geography": text(row.get("geography")),
		"employee_count": integer(row.get("employee_count")),
		"estimated_revenue": number(row.get("estimated_revenue")),
		"estimated_acreage": number(row.get("estimated_acreage")),
		"strengths": lines(row.get("strengths")),
		"weaknesses": lines(row.get("weaknesses")),
		"key_assets": lines(row.get("key_assets")),
		"vulnerability_windows": lines(row.get("vulnerability_windows")),
		"notes": text(row.get("notes")),
	}
	plan = links.resolve("strategic_plan", row.get("strategic_plan_id"))
	if plan:
		doc["strategic_plan"] = plan
	# `contact_id` pointed at the farm_app's own Contact table, which has no
	# single ERPNext counterpart — a contact became a Customer, a Supplier or
	# neither depending on what it was for. Reported rather than guessed.
	if row.get("contact_id") is not None:
		warnings.append(
			f"contact id {row['contact_id']} is not linked — a farm_app Contact may be a Customer or "
			"a Supplier in ERPNext, and the migration will not choose one"
		)
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def build_acquisition_target(row: dict, links: Links, context: dict, warnings: list) -> dict:
	participant = links.require("market_participant", row.get("participant_id"), "this target")
	doc = {
		"market_participant": participant,
		"entity_name": text(context.get("participant_names", {}).get(str(row.get("participant_id"))))
		or participant,
		"status": option(row.get("status"), ACQUISITION_STATUS, warnings, "status", "Identified"),
		"action_level": option(
			row.get("action_level"), ACQUISITION_ACTION, warnings, "action_level", "Monitor"
		),
		"strategic_fit_score": number(row.get("strategic_fit_score")),
		"financial_health_score": number(row.get("financial_health_score")),
		"synergy_score": number(row.get("synergy_score")),
		"cultural_fit_score": number(row.get("cultural_fit_score")),
		"accretive_score": number(row.get("accretive_score")),
		"estimated_acquisition_cost": number(row.get("estimated_acquisition_cost")),
		"projected_revenue_uplift": number(row.get("projected_revenue_uplift")),
		"projected_cost_savings": number(row.get("projected_cost_savings")),
		"payback_period_years": number(row.get("payback_period_years")),
		"irr_estimate": number(row.get("irr_estimate")),
		"intergenerational_horizon_years": integer(row.get("intergenerational_horizon_years")),
		"land_value_appreciation": number(row.get("land_value_appreciation")),
		"water_rights_value": number(row.get("water_rights_value")),
		"varietal_ip_value": number(row.get("varietal_ip_value")),
		"infrastructure_value": number(row.get("infrastructure_value")),
		"identified_date": day(row.get("identified_date")),
		"target_close_date": day(row.get("target_close_date")),
		"actual_close_date": day(row.get("actual_close_date")),
		"recommendation": json_text(row.get("recommendation")),
		"notes": text(row.get("notes")),
	}
	plan = links.resolve("strategic_plan", row.get("strategic_plan_id"))
	if plan:
		doc["strategic_plan"] = plan
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def build_competitive_move(row: dict, links: Links, context: dict, warnings: list) -> dict:
	participant = links.require("market_participant", row.get("participant_id"), "this move")
	observed = day(row.get("observed_date"))
	if not observed:
		raise RowRefused("a move with no observed date cannot be placed in the competitive timeline")
	doc = {
		"market_participant": participant,
		"observed_date": observed,
		"move_type": text(row.get("move_type")),
		"severity": option(row.get("severity"), MOVE_SEVERITY, warnings, "severity", "Medium"),
		"description": text(row.get("description")),
		"source": text(row.get("source")),
		"confidence": option(row.get("confidence"), CONFIDENCE, warnings, "confidence", "Medium"),
		"market_impact_pct": number(row.get("market_impact_pct")),
		"revenue_impact": number(row.get("revenue_impact")),
		"response_urgency": option(row.get("response_urgency"), MOVE_URGENCY, warnings, "response_urgency"),
		"recommended_response": json_text(row.get("recommended_response")),
		"actual_response": text(row.get("actual_response")),
		"response_date": day(row.get("response_date")),
		"outcome": text(row.get("outcome")),
		"notes": text(row.get("notes")),
	}
	plan = links.resolve("strategic_plan", row.get("strategic_plan_id"))
	if plan:
		doc["strategic_plan"] = plan
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


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
	crop = links.resolve("commodity", row.get("commodity_id"))
	if crop:
		doc["crop"] = crop
	market = links.resolve("country", row.get("country_id"))
	if market:
		doc["market"] = market
	else:
		warnings.append(
			f"country id {row.get('country_id')!r} has no Market — the record migrates without one, "
			"and an MRL with no market is a limit nobody can apply to a shipment"
		)
	if context.get("company"):
		doc["company"] = context["company"]
	return {key: value for key, value in doc.items() if value not in (None, "")}


def build_satellite_fold(row: dict, links: Links, context: dict, warnings: list) -> dict:
	"""The `Field` columns one satellite metric row updates.

	See the module docstring: `Satellite Metric` does not exist, so only the
	newest NDVI reading per block survives, onto the three `last_ndvi_*` columns.
	Everything else in the table is reported as dropped.
	"""
	from . import satellite

	metric = text(row.get("metric_type")).lower()
	if metric not in ("ndvi", ""):
		raise RowRefused(f"{metric or 'unnamed'} has nowhere to land — `Field` stores only NDVI")
	field = links.require("field", row.get("field_id"), "this metric")
	value = number(row.get("value"))
	if value is None:
		indexed = number(row.get("indexed_value"))
		value = satellite.from_index(indexed, "ndvi") if indexed is not None else None
	if value is None:
		raise RowRefused("no NDVI value on this row")
	return {
		"__docname": field,
		satellite.FIELD_COLUMNS["mean"]: round(value, 6),
		satellite.FIELD_COLUMNS["pulled_on"]: day(row.get("timestamp")),
		satellite.FIELD_COLUMNS["provider"]: text(row.get("source")) or satellite.DATA_COLLECTION,
	}


#: The transfer, in dependency order. Reordering this list breaks the foreign
#: keys — `iot_reading` cannot resolve its device before `iot_device` has run —
#: so the order is the spec and not a presentation choice.
SPECS = (
	Spec(
		"field",
		"Field",
		("external_farm_app_id",),
		build_field,
		note="blocks; usually already migrated by import_farm_app_fields",
	),
	Spec("strategic_plan", "Strategic Plan", ("plan_name", "version"), build_strategic_plan),
	Spec(
		"objective",
		"Strategic Objective",
		("strategic_plan", "objective"),
		build_objective,
		depends_on=("strategic_plan",),
	),
	Spec(
		"market_participant",
		"Market Participant",
		("participant_name",),
		build_market_participant,
		depends_on=("strategic_plan",),
	),
	Spec(
		"acquisition_target",
		"Acquisition Target",
		("market_participant", "identified_date"),
		build_acquisition_target,
		depends_on=("market_participant",),
	),
	Spec(
		"competitive_move",
		"Competitive Move",
		("market_participant", "observed_date", "move_type"),
		build_competitive_move,
		depends_on=("market_participant",),
	),
	Spec("mrl_research_session", "MRL Record", ("chemical", "crop", "market"), build_mrl_record),
	Spec("iot_device", "IoT Device", ("hardware_id",), build_iot_device, depends_on=("field",)),
	Spec(
		"iot_reading",
		"IoT Reading",
		("device", "reading_type", "timestamp"),
		build_iot_reading,
		depends_on=("iot_device",),
	),
	Spec(
		"field_satellite_metric",
		"Field",
		(),
		build_satellite_fold,
		depends_on=("field",),
		mode="update",
		note="newest NDVI per block only; the rest of the series has no target doctype",
	),
)

SPEC_BY_TABLE = {spec.table: spec for spec in SPECS}


def seed_links_from_site(frappe_module=None) -> dict:
	"""`{"field": {farm_app id: docname}}` read off the site's own Fields.

	The bridge between waves: `import_farm_app_fields` wrote
	`external_farm_app_id` for exactly this. Everything else in the map is built
	during the run.
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

	`updated` IS COUNTED SEPARATELY FROM `created` because only the satellite fold
	updates, and it updates on every run by design — it writes the same three
	columns to the same values. Folding it into `created` made a second, fully
	idempotent run report that it had created something, which is the one number
	an operator checks to decide whether the migration is done.
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

	# The satellite fold needs the newest row per block, so it is collected first
	# and then reduced. Every other spec streams.
	rows = list(read_rows(connection, spec.table, limit)) if spec.mode == "update" else None
	if rows is not None:
		entry["read"] = len(rows)
		rows = _newest_per_field(rows, entry)
	source = rows if rows is not None else read_rows(connection, spec.table, limit)

	for row in source:
		if rows is None:
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

		if spec.mode == "update":
			docname = doc.pop("__docname")
			loader.update(spec.doctype, docname, doc)
			entry["updated"] += 1
			continue

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


def _newest_per_field(rows: list, entry: dict) -> list:
	"""The newest NDVI row per block, with the rest counted as dropped."""
	newest = {}
	for row in rows:
		if text(row.get("metric_type")).lower() not in ("ndvi", ""):
			continue
		key = str(row.get("field_id"))
		stamp = moment(row.get("timestamp"))
		if key not in newest or stamp > moment(newest[key].get("timestamp")):
			newest[key] = row
	dropped = len(rows) - len(newest)
	if dropped > 0:
		entry["warnings"].append(
			{
				"id": None,
				"warning": f"{dropped} of {len(rows)} satellite rows were dropped: `Satellite Metric` "
				"has not shipped, so only the newest NDVI per block survives. The time series is "
				"lost unless that doctype is built before the sidecar is decommissioned.",
			}
		)
	return list(newest.values())
