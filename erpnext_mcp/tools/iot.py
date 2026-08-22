# SPDX-License-Identifier: MIT
"""The device register and the readings that come off it.

WHAT A READING IS WORTH DEPENDS ENTIRELY ON THE DEVICE, which is why every read
tool here reports the device's health beside the numbers rather than leaving a
caller to go and ask. A soil moisture series that flatlines at 11% is a dry block
if the probe is healthy and a dead battery if it is not, and those two readings of
the same data lead to opposite irrigation decisions.

`last_seen` IS WRITTEN BY `create_iot_reading` AND BY NOTHING ELSE. Not by
`update_iot_device`, which is the tool an operator would otherwise use to make a
sensor look alive without meaning to. The column means "this device actually
spoke", and the moment anything else can set it, it stops meaning that — so the
device controller refuses to touch it and the reading path is the only writer.

ONLINE IS COMPUTED AT READ TIME AND NEVER STORED. A stored flag is wrong from the
moment a device goes quiet, because nothing runs to update it — and the moment it
goes quiet is the only moment the flag matters.

THE READING LIST DOES NOT AGGREGATE AND `get_device_readings` DOES. That split is
deliberate: a list is rows, and a caller that wanted a mean of forty thousand of
them should not have to page through forty thousand rows to compute one. What
`get_device_readings` will NOT do is average across reading types or across
units — a mean of a temperature and a flow rate is a number with no meaning, and
producing one is worse than refusing.
"""

import frappe

from .. import compat
from ..args import as_bool, as_choice, as_float, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.iot_device.iot_device import ONLINE_WINDOW_SECONDS
from ..errors import ToolError
from ..result import ToolResult

DEVICE = "IoT Device"
READING = "IoT Reading"

#: How many rows any one register call will return at most, whatever limit is
#: asked for. Readings are the highest-volume record this app writes and an
#: unbounded page of them is a way to take a site down by accident.
REGISTER_CAP = 500

_DEVICE_FIELDS = (
	"name",
	"company",
	"device_name",
	"hardware_id",
	"device_type",
	"device_class",
	"field",
	"zone",
	"enabled",
	"last_seen",
	"battery_level",
	"signal_strength",
	"calibrated_on",
	"device_config",
	"notes",
)

_READING_FIELDS = (
	"name",
	"device",
	"company",
	"field",
	"timestamp",
	"reading_type",
	"value",
	"unit",
	"quality",
	"notes",
)

#: Below this the battery is reported as low. Fifteen percent rather than zero
#: because a probe reports DRIFTING values before it reports nothing, and the
#: window between the two is where the bad data comes from.
BATTERY_LOW_PCT = 15.0


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


def _seconds_since(when) -> float | None:
	if not when:
		return None
	return frappe.utils.time_diff_in_seconds(frappe.utils.now(), str(when))


def _describe_device(row: dict) -> dict:
	silent = _seconds_since(row.get("last_seen"))
	battery = row.get("battery_level")
	battery = float(battery) if battery not in (None, "") else None
	described = {
		"name": row.get("name"),
		"company": row.get("company"),
		"device_name": row.get("device_name"),
		"hardware_id": row.get("hardware_id"),
		"device_type": row.get("device_type"),
		"device_class": row.get("device_class") or None,
		"field": row.get("field") or None,
		"zone": row.get("zone") or None,
		"enabled": compat.checked(row.get("enabled")),
		"last_seen": str(row["last_seen"]) if row.get("last_seen") else None,
		"seconds_since_seen": int(silent) if silent is not None else None,
		# Computed here, on every call, and stored nowhere. See the module docstring.
		"online": silent is not None and silent <= ONLINE_WINDOW_SECONDS,
		"battery_level": battery,
		"battery_low": battery is not None and battery < BATTERY_LOW_PCT,
		"signal_strength": float(row["signal_strength"])
		if row.get("signal_strength") not in (None, "")
		else None,
		"calibrated_on": str(row["calibrated_on"]) if row.get("calibrated_on") else None,
		"device_config": row.get("device_config") or None,
		"notes": row.get("notes") or None,
	}
	described["health_warnings"] = _health_warnings(described)
	return described


def _health_warnings(device: dict) -> list:
	"""What is wrong with this device, in the words somebody would need.

	A device that has NEVER been heard from is called out separately from one
	that has gone quiet: the first is almost always a registration that was never
	finished, and the second is a battery, a radio or a tractor.
	"""
	warnings = []
	if not device["enabled"]:
		warnings.append("This device is disabled. Its readings are historical.")
	elif device["last_seen"] is None:
		warnings.append(
			"This device has never reported. It is registered and nothing has ever arrived "
			"from it — check the hardware id against the unit and the token it was given."
		)
	elif not device["online"]:
		hours = round((device["seconds_since_seen"] or 0) / 3600, 1)
		warnings.append(
			f"Silent for {hours} hours. A probe that is not reporting is not reading zero — "
			"anything computed from its latest value is that old."
		)
	if device["battery_low"]:
		warnings.append(
			f"Battery at {device['battery_level']}%. Readings drift before they stop, so the "
			"recent values from this device are the ones to distrust."
		)
	if device["calibrated_on"] is None and device["device_class"] != "Gateway":
		warnings.append("Never calibrated. An uncalibrated probe is a number without units.")
	return warnings


def _describe_reading(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"device": row.get("device"),
		"company": row.get("company") or None,
		"field": row.get("field") or None,
		"timestamp": str(row["timestamp"]) if row.get("timestamp") else None,
		"reading_type": row.get("reading_type"),
		"value": float(row.get("value") or 0),
		"unit": row.get("unit"),
		"quality": row.get("quality") or None,
		"notes": row.get("notes") or None,
	}


def _latest_reading(device: str) -> dict:
	rows = frappe.db.get_all(
		READING,
		filters={"device": device},
		fields=compat.existing_fields(READING, _READING_FIELDS),
		order_by="timestamp desc",
		limit=1,
	)
	return _describe_reading(dict(rows[0])) if rows else {}


# ── create_iot_device ───────────────────────────────────────────────────────
def create_iot_device(args: dict) -> ToolResult:
	"""Register one device, and mint the token it will authenticate with."""
	_require(DEVICE)
	company = resolve_company(as_str(args, "company"), required=True)

	doc = frappe.new_doc(DEVICE)
	doc.company = company
	doc.device_name = as_str(args, "device_name", required=True)
	doc.hardware_id = as_str(args, "hardware_id", required=True)
	doc.device_type = as_choice(
		DEVICE, "device_type", as_str(args, "device_type", required=True), "device_type"
	)
	device_class = as_str(args, "device_class")
	if device_class:
		doc.device_class = as_choice(DEVICE, "device_class", device_class, "device_class")

	field = as_str(args, "field")
	if field:
		if not frappe.db.exists("Field", field):
			raise ToolError(f"Field {field!r} does not exist. Nothing was created.")
		doc.field = field
	doc.zone = as_str(args, "zone")
	doc.notes = as_str(args, "notes")
	doc.device_config = as_str(args, "device_config")
	doc.calibrated_on = as_str(args, "calibrated_on")

	enabled = as_bool(args, "enabled", default=True)
	doc.enabled = 1 if enabled else 0

	# The token is generated here and never accepted from the caller. A token
	# somebody chose is a token somebody can guess, and this one authenticates a
	# device that will be posting unattended for years.
	token = frappe.generate_hash(length=48)
	doc.auth_token = token
	doc.insert(ignore_permissions=True)

	described = _describe_device(dict(doc.as_dict()))
	return ToolResult(
		data={
			**described,
			"auth_token": token,
			"next_step": (
				"This is the only time the token is shown. Put it on the device now — nothing "
				"reads it back, and a device that loses it has to be re-registered."
			),
		},
		summary=f"registered {doc.name}: {described['device_name']} ({described['device_type']})",
		docstatus_delta="none → 0 (created)",
	)


# ── get_iot_device ──────────────────────────────────────────────────────────
def get_iot_device(args: dict) -> ToolResult:
	"""One device in full, with its health and its most recent reading."""
	_require(DEVICE)
	name = as_str(args, "device", required=True)
	row = frappe.db.get_value(DEVICE, name, compat.existing_fields(DEVICE, _DEVICE_FIELDS), as_dict=True)
	if not row:
		row = frappe.db.get_value(
			DEVICE, {"hardware_id": name}, compat.existing_fields(DEVICE, _DEVICE_FIELDS), as_dict=True
		)
	if not row:
		raise ToolError(f"No IoT Device {name!r} — tried it as a docname and as a hardware id.")

	described = _describe_device(dict(row))
	described["latest_reading"] = _latest_reading(described["name"]) or None
	if compat.doctype_exists(READING):
		described["reading_count"] = frappe.db.count(READING, {"device": described["name"]})
	return ToolResult(
		data=described,
		summary=(
			f"{described['name']}: {described['device_type']}, "
			f"{'online' if described['online'] else 'not online'}"
		),
	)


# ── list_iot_devices ────────────────────────────────────────────────────────
def list_iot_devices(args: dict) -> ToolResult:
	"""The device register, with the offline and low-battery ones named."""
	_require(DEVICE)
	company = resolve_company(as_str(args, "company"))
	limit = as_limit(args)

	filters = {}
	if company:
		filters["company"] = company
	field = as_str(args, "field")
	if field:
		filters["field"] = field
	device_type = as_str(args, "device_type")
	if device_type:
		filters["device_type"] = as_choice(DEVICE, "device_type", device_type, "device_type")
	device_class = as_str(args, "device_class")
	if device_class:
		filters["device_class"] = as_choice(DEVICE, "device_class", device_class, "device_class")
	enabled = as_bool(args, "enabled")
	if enabled is not None:
		filters["enabled"] = 1 if enabled else 0

	rows = frappe.db.get_all(
		DEVICE,
		filters=filters,
		fields=compat.existing_fields(DEVICE, _DEVICE_FIELDS),
		order_by="device_name asc",
		limit=min(limit, REGISTER_CAP),
	)
	devices = [_describe_device(dict(row)) for row in rows]

	online_only = as_bool(args, "online")
	if online_only is not None:
		devices = [row for row in devices if row["online"] is bool(online_only)]

	by_type: dict = {}
	for row in devices:
		by_type[row["device_type"]] = by_type.get(row["device_type"], 0) + 1

	return ToolResult(
		data={
			"company": company,
			"device_count": len(devices),
			"online_count": sum(1 for row in devices if row["online"]),
			"by_type": dict(sorted(by_type.items())),
			# Named rather than merely counted, because "three are offline" is not
			# actionable and "the two probes in Yellow Camp are offline" is.
			"offline": [row["name"] for row in devices if row["enabled"] and not row["online"]],
			"never_reported": [row["name"] for row in devices if row["last_seen"] is None],
			"battery_low": [row["name"] for row in devices if row["battery_low"]],
			"devices": devices,
		},
		summary=f"{len(devices)} device(s), {sum(1 for row in devices if row['online'])} online",
	)


# ── update_iot_device ───────────────────────────────────────────────────────
def update_iot_device(args: dict) -> ToolResult:
	"""Change a device's placement, health figures or config. Cannot re-key it."""
	_require(DEVICE)
	name = as_str(args, "device", required=True)
	if not frappe.db.exists(DEVICE, name):
		raise ToolError(f"IoT Device {name!r} does not exist. Nothing was changed.")

	if "last_seen" in args:
		raise ToolError(
			"last_seen cannot be set by hand. It is written only when a reading actually "
			"arrives, so that it always means the device spoke — a last_seen somebody typed is "
			"the one thing that would make a dead sensor look alive. Nothing was changed."
		)
	if "auth_token" in args:
		raise ToolError(
			"auth_token cannot be changed here. Nothing reads it back, so rotating it means "
			"re-registering the device with the new token in hand. Nothing was changed."
		)

	doc = frappe.get_doc(DEVICE, name)
	changes = {}

	def stage(key, value):
		before = doc.get(key)
		if not _same(before, value):
			changes[key] = [before, value]
			doc.set(key, value)

	for key in ("device_name", "zone", "device_config", "notes"):
		if key in args:
			stage(key, as_str(args, key))
	if "hardware_id" in args:
		stage("hardware_id", as_str(args, "hardware_id", required=True))
	if "device_type" in args:
		stage("device_type", as_choice(DEVICE, "device_type", as_str(args, "device_type"), "device_type"))
	if "device_class" in args:
		stage("device_class", as_choice(DEVICE, "device_class", as_str(args, "device_class"), "device_class"))
	if "field" in args:
		field = as_str(args, "field")
		if field and not frappe.db.exists("Field", field):
			raise ToolError(f"Field {field!r} does not exist. Nothing was changed.")
		stage("field", field)
	if "calibrated_on" in args:
		stage("calibrated_on", as_str(args, "calibrated_on"))
	for key in ("battery_level", "signal_strength"):
		if key in args:
			stage(key, as_float(args.get(key), key))
	if "enabled" in args:
		stage("enabled", 1 if as_bool(args, "enabled") else 0)

	if not changes:
		raise ToolError(
			"nothing to change. Pass at least one of: device_name, hardware_id, device_type, "
			"device_class, field, zone, enabled, battery_level, signal_strength, calibrated_on, "
			"device_config, notes."
		)

	doc.save(ignore_permissions=True)
	described = _describe_device(dict(doc.as_dict()))
	return ToolResult(
		data={**described, "changed": changes},
		summary=f"{doc.name}: {len(changes)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


# ── create_iot_reading ──────────────────────────────────────────────────────
def create_iot_reading(args: dict) -> ToolResult:
	"""Record one measurement, and mark the device as having spoken."""
	_require(DEVICE)
	_require(READING)

	device = as_str(args, "device", required=True)
	row = frappe.db.get_value(DEVICE, device, ["name", "enabled"], as_dict=True)
	if not row:
		resolved = frappe.db.get_value(DEVICE, {"hardware_id": device}, ["name", "enabled"], as_dict=True)
		if not resolved:
			raise ToolError(
				f"No IoT Device {device!r} — tried it as a docname and as a hardware id. "
				"Nothing was recorded."
			)
		row = resolved
	device = row["name"]

	if not compat.checked(row.get("enabled")):
		raise ToolError(
			f"{device} is disabled, so this reading has nowhere honest to go: a disabled device "
			"is one somebody took out of service, and accepting readings from it would make its "
			"retirement invisible. Re-enable it with update_iot_device if it is back in the "
			"field. Nothing was recorded."
		)

	doc = frappe.new_doc(READING)
	doc.device = device
	doc.timestamp = as_str(args, "timestamp", required=True)
	doc.reading_type = as_choice(
		READING, "reading_type", as_str(args, "reading_type", required=True), "reading_type"
	)
	if args.get("value") is None:
		raise ToolError("value is required. Nothing was recorded.")
	doc.value = as_float(args.get("value"), "value")
	doc.unit = as_str(args, "unit", required=True)
	quality = as_str(args, "quality")
	if quality:
		doc.quality = as_choice(READING, "quality", quality, "quality")
	doc.notes = as_str(args, "notes")
	doc.insert(ignore_permissions=True)

	# The one place last_seen is written. `update_modified=False` because a
	# reading arriving is not somebody editing the device, and letting it bump
	# the device's modified stamp would make every sync tool re-read the whole
	# register every fifteen minutes.
	frappe.db.set_value(DEVICE, device, "last_seen", doc.timestamp, update_modified=False)

	described = _describe_reading(dict(doc.as_dict()))
	return ToolResult(
		data={**described, "device_last_seen": str(doc.timestamp)},
		summary=f"{device}: {described['reading_type']} {described['value']} {described['unit']}",
		docstatus_delta="none → 0 (created)",
	)


def _reading_filters(args: dict) -> dict:
	filters = {}
	device = as_str(args, "device")
	if device:
		resolved = (
			device
			if frappe.db.exists(DEVICE, device)
			else frappe.db.get_value(DEVICE, {"hardware_id": device}, "name")
		)
		if not resolved:
			raise ToolError(f"No IoT Device {device!r} — tried it as a docname and as a hardware id.")
		filters["device"] = resolved
	field = as_str(args, "field")
	if field:
		filters["field"] = field
	company = resolve_company(as_str(args, "company"))
	if company:
		filters["company"] = company
	reading_type = as_str(args, "reading_type")
	if reading_type:
		filters["reading_type"] = as_choice(READING, "reading_type", reading_type, "reading_type")
	quality = as_str(args, "quality")
	if quality:
		filters["quality"] = as_choice(READING, "quality", quality, "quality")

	from_ts = as_str(args, "from_timestamp") or as_str(args, "from_date")
	to_ts = as_str(args, "to_timestamp") or as_str(args, "to_date")
	if from_ts and to_ts:
		filters["timestamp"] = ("between", [from_ts, to_ts])
	elif from_ts:
		filters["timestamp"] = (">=", from_ts)
	elif to_ts:
		filters["timestamp"] = ("<=", to_ts)
	return filters


# ── list_iot_readings ───────────────────────────────────────────────────────
def list_iot_readings(args: dict) -> ToolResult:
	"""Readings matching the filters, newest first. Rows, not statistics."""
	_require(READING)
	limit = as_limit(args)
	filters = _reading_filters(args)

	rows = frappe.db.get_all(
		READING,
		filters=filters,
		fields=compat.existing_fields(READING, _READING_FIELDS),
		order_by="timestamp desc",
		limit=min(limit, REGISTER_CAP),
	)
	readings = [_describe_reading(dict(row)) for row in rows]

	by_quality: dict = {}
	for row in readings:
		key = row["quality"] or "(unrecorded)"
		by_quality[key] = by_quality.get(key, 0) + 1

	return ToolResult(
		data={
			"reading_count": len(readings),
			"capped_at": REGISTER_CAP if len(readings) >= REGISTER_CAP else None,
			"by_quality": dict(sorted(by_quality.items())),
			"readings": readings,
		},
		summary=f"{len(readings)} reading(s)",
	)


# ── get_device_readings ─────────────────────────────────────────────────────
def get_device_readings(args: dict) -> ToolResult:
	"""One device's readings over a window, summarised per reading type.

	SUMMARISED PER TYPE AND PER UNIT, NEVER ACROSS THEM. A device reporting both
	soil temperature and soil moisture has two series, and a single mean over the
	pair is a number with no referent. Where one reading type arrives in two
	different units — which happens when a device is reconfigured mid-season —
	that is reported as a fact rather than averaged through.
	"""
	_require(DEVICE)
	_require(READING)

	device = as_str(args, "device", required=True)
	row = frappe.db.get_value(DEVICE, device, compat.existing_fields(DEVICE, _DEVICE_FIELDS), as_dict=True)
	if not row:
		row = frappe.db.get_value(
			DEVICE, {"hardware_id": device}, compat.existing_fields(DEVICE, _DEVICE_FIELDS), as_dict=True
		)
	if not row:
		raise ToolError(f"No IoT Device {device!r} — tried it as a docname and as a hardware id.")
	described = _describe_device(dict(row))

	filters = _reading_filters({**args, "device": described["name"]})
	rows = frappe.db.get_all(
		READING,
		filters=filters,
		fields=compat.existing_fields(READING, _READING_FIELDS),
		order_by="timestamp desc",
		limit=REGISTER_CAP,
	)
	readings = [_describe_reading(dict(row)) for row in rows]

	series: dict = {}
	for reading in readings:
		bucket = series.setdefault(
			reading["reading_type"],
			{"count": 0, "units": set(), "values": [], "first": None, "last": None},
		)
		bucket["count"] += 1
		bucket["units"].add(reading["unit"])
		bucket["values"].append(reading["value"])
		stamp = reading["timestamp"]
		if bucket["last"] is None or (stamp or "") > bucket["last"]:
			bucket["last"] = stamp
		if bucket["first"] is None or (stamp or "") < bucket["first"]:
			bucket["first"] = stamp

	summarised = {}
	for reading_type, bucket in sorted(series.items()):
		units = sorted(unit for unit in bucket["units"] if unit)
		values = bucket["values"]
		summarised[reading_type] = {
			"count": bucket["count"],
			"units": units,
			"mixed_units": len(units) > 1,
			"min": round(min(values), 4) if values else None,
			"max": round(max(values), 4) if values else None,
			"mean": round(sum(values) / len(values), 4) if values else None,
			"latest": values[0] if values else None,
			"first_timestamp": bucket["first"],
			"last_timestamp": bucket["last"],
			"note": (
				f"Two units appear in this series ({', '.join(units)}). The figures above span "
				"both and cannot be read as one measurement — the device was almost certainly "
				"reconfigured mid-window."
			)
			if len(units) > 1
			else None,
		}

	return ToolResult(
		data={
			"device": described,
			"from": as_str(args, "from_timestamp") or as_str(args, "from_date") or None,
			"to": as_str(args, "to_timestamp") or as_str(args, "to_date") or None,
			"reading_count": len(readings),
			"capped_at": REGISTER_CAP if len(readings) >= REGISTER_CAP else None,
			"by_reading_type": summarised,
			"readings": readings,
		},
		summary=(f"{described['name']}: {len(readings)} reading(s) across {len(summarised)} type(s)"),
	)
