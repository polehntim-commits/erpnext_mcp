# SPDX-License-Identifier: MIT
"""APNs, and the break horn that has to reach a crew rather than a foreman.

WHAT THIS IS FOR. `BreakAlarm` on the handset plays a tone the moment a foreman
calls a break — over an `AVAudioSession` in `.playback`, so it rings through the
silent switch, because a break horn that respects a muted phone does not work on
most of a crew's phones. That is exactly one phone: the one the break was called
on. Every other worker on the shift learns about the break when somebody shouts.
This module is the other twenty phones.

v0.140.0 ADDS THE ONE THAT GOES THE OTHER WAY. `heat_payload` reaches ONE phone
— the crew leader's — and it is the only push in this app addressed to a single
named person rather than to a crew or to a role. The weather sweep already knew
at 11:45 that the block had crossed the heat threshold; the foreman found out by
opening the app. See `services/weather.evaluate_thresholds` for where it fires
and `heat_payload` for why it is allowed to pierce Do Not Disturb when a nightly
compliance alert is not.

────────────────────────────────────────────────────────────────────────────
IT DEGRADES TO NOTHING, AND THAT IS THE DESIGN
────────────────────────────────────────────────────────────────────────────

A p8 signing key is an operator artefact: somebody downloads it from Apple once,
puts it on the bench, and never touches it again. Until that has happened this
module has no way to send anything, and the thing it must not do in the meantime
is take a break log down with it. So:

  * Every entry point returns a REPORT and never raises. `sent`, `skipped`,
    `failed` and a `reason` naming which of those it was.
  * An unconfigured site is `skipped` with `reason: "not_configured"` and one
    Error Log line, not an exception and not a silent zero. The distinction
    matters when a foreman asks why nobody's phone buzzed: "no key on this
    bench" and "sent to nobody because the crew has no tokens" are different
    problems with different people to go and see.
  * A missing HTTP/2 client is the same shape. APNs is HTTP/2-only, `requests`
    speaks HTTP/1.1, and this app does not add a dependency to ship a feature
    the site cannot use yet — so `httpx` is imported defensively and its absence
    is a named skip, exactly as Pillow's is in `wallet.py`.

────────────────────────────────────────────────────────────────────────────
THE KEY LIVES IN site_config.json AND NOT IN A SETTINGS DOCTYPE
────────────────────────────────────────────────────────────────────────────

The same argument `wallet.apple_config` makes, for the same reason. A Single
doctype is editable by anybody who reaches the Desk with the right role and is
dumped in full by a dozen Frappe debug paths; `site_config.json` is a file on
the bench that only the operator who deploys the site can write. An ES256
private key belongs in the second kind of place.

`configured` is FOUR separate facts ANDed rather than one flag an operator could
tick while leaving the key out: without the key there is nothing to sign with,
without the key id Apple cannot tell which key signed, without the team id the
token names no developer account, and without the topic the push is addressed to
no app. A push missing any one of them is rejected by Apple with a 403 that says
nothing useful, which is a worse failure than not sending.

────────────────────────────────────────────────────────────────────────────
WHY THE TRANSPORT IS AN ARGUMENT
────────────────────────────────────────────────────────────────────────────

`send_push` takes a `transport` callable. The default one talks to Apple; the
suite passes a fake. That seam is here rather than a `unittest.mock.patch` on a
module global because the dispatch decisions this module makes — which tokens to
address, what to do with a 410, whether a crew break with no tokens is a failure
— are the part worth testing, and they are unreachable behind a real HTTP client
that no test may call.
"""

from __future__ import annotations

import json
import time

import frappe

from .. import compat, roles

TOKEN_DOCTYPE = "Mobile Push Token"
SHIFT_DOCTYPE = "Farm Shift"
EMPLOYEE_DOCTYPE = "Employee"
ROLE_ROW_DOCTYPE = "Has Role"

#: Apple's two hosts. `sandbox` is what a development build's tokens are minted
#: against, and a token from one host is meaningless on the other — a push to
#: the wrong one comes back BadDeviceToken, which reads exactly like a stale
#: token and is not one. Hence `apns_environment` being explicit configuration
#: rather than something inferred.
HOSTS = {
	"production": "https://api.push.apple.com",
	"sandbox": "https://api.sandbox.push.apple.com",
}

#: How long a provider JWT is reused. Apple rejects a token older than an hour
#: and rate-limits a provider that mints a fresh one per push, so the window is
#: deliberately in the middle of those two failures rather than at either end.
JWT_LIFETIME_SECONDS = 45 * 60

#: The two sounds the app has bundled. Named here because the server chooses
#: which one plays: a rising double blast when a break starts, a descending
#: triple pip when it ends. Deliberately unlike each other and unlike any system
#: sound — a worker in an orchard has to know which one they just heard without
#: taking the phone out.
SOUND_BREAK_START = "break_start.caf"
SOUND_BREAK_END = "break_end.caf"

#: `time-sensitive` is the whole point of the payload. A break horn is worthless
#: if Focus or a Scheduled Summary holds it until lunchtime, and this is the
#: interruption level Apple provides for exactly this case. It requires the
#: entitlement on the app side, which the handset already ships.
INTERRUPTION_LEVEL = "time-sensitive"

#: v0.107.0. THE LEVEL FOR EVERYTHING THAT IS NOT A BREAK HORN, and the choice is
#: deliberate rather than a default nobody thought about. `time-sensitive` pierces
#: Focus and Do Not Disturb; a break horn earns that because stopping work when
#: relief is called is a safety obligation with a clock on it. A task dispatched
#: for tomorrow morning and a compliance alert raised by a sweep that runs at two
#: in the morning do not. `active` still lights the screen and makes a sound when
#: the phone is not silenced — and stays quiet, in the list, until morning when it
#: is. A server that overrode a foreman's Do Not Disturb nightly would be trained
#: out of by the second week, and then the break horn would be ignored too.
INTERRUPTION_ACTIVE = "active"

#: The system sound. A task and an alert do NOT get one of the two break tones:
#: those two are learned sounds that mean "stop work" and "resume", and spending
#: them on a notification about paperwork is how they stop meaning anything.
SOUND_DEFAULT = "default"

#: `apns-priority`. 10 is "deliver immediately"; 5 lets Apple hold delivery to a
#: moment that conserves the handset's power. A break and a dispatch are worth 10
#: — somebody is being asked to do something now. A compliance alert raised by the
#: nightly sweep is worth 5: it is news for the morning, and a battery spent
#: waking a phone at 02:00 to say a certificate expires in a fortnight is spent
#: badly.
PRIORITY_IMMEDIATE = "10"
PRIORITY_CONSIDERATE = "5"

#: The `aps.category` values, which are what the handset switches on to decide
#: which notification actions to offer and which code path to play the payload
#: through. Named here rather than spelled at each call site so the server and
#: `fafo_ios` have one list to disagree about instead of four.
CATEGORY_BREAK = "FARM_BREAK"
CATEGORY_TEST = "FARM_TEST"
CATEGORY_TASK = "FARM_TASK"
CATEGORY_COMPLIANCE = "FARM_COMPLIANCE"
CATEGORY_HEAT = "FARM_HEAT"

#: How much prose survives into a notification body. An APNs payload is capped at
#: 4KB and a `Compliance Alert.alert_message` has no such cap — the rules compose
#: whole sentences with record names in them. Trimming here rather than letting
#: Apple reject the whole push is the difference between a shortened alert and no
#: alert; the full text is one tap away in the app, which is where the docname in
#: the payload sends it.
MAX_BODY = 240

#: What Apple answers when the token is dead. BOTH DEACTIVATE THE ROW and
#: nothing else does: `Unregistered` means the app was deleted, `BadDeviceToken`
#: means the string is not a token for this topic and environment. Everything
#: else — `TooManyRequests`, `InternalServerError`, `ServiceUnavailable` — is
#: about Apple or about this farm's configuration, and deactivating a worker's
#: phone because Apple had a bad afternoon would silently unsubscribe a crew.
DEAD_TOKEN_REASONS = frozenset({"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic"})

#: The sentence an operator is told when nothing can be sent. Names the keys,
#: not the concept — "configure APNs" is not an actionable instruction.
APNS_REQUIREMENTS = (
	"an Apple Push Notification key. Set `apns_key` (the contents of the .p8, or a path "
	"to it), `apns_key_id` (the ten-character Key ID Apple showed when it was created), "
	"`apns_team_id` (the ten-character Team ID) and `apns_topic` (the app's bundle "
	"identifier) in the site's site_config.json, plus `apns_environment` as production or "
	"sandbox. Until then breaks are logged exactly as before and no push is attempted."
)


# ── logging ─────────────────────────────────────────────────────────────────


def _log(message: str) -> None:
	"""Say what went wrong, without ever being the thing that goes wrong.

	`frappe.log_error` writes an Error Log row, which is a database write, which
	is a thing that can fail on the site where this is already failing. The break
	that could not be pushed is still a break that was logged.
	"""
	try:
		frappe.log_error(title="erpnext_mcp: push", message=message)
	except Exception:  # pragma: no cover - a site that cannot write its own Error Log
		pass


# ── configuration ───────────────────────────────────────────────────────────


def _text(conf, key: str, default: str = "") -> str:
	return str((conf or {}).get(key) or default).strip()


def _key_material(raw: str) -> str:
	"""The p8 contents, whether the operator configured the text or a path to it.

	Both spellings are accepted because both are reasonable and an operator
	should not have to guess which one this app wanted. A path that cannot be
	read comes back empty, which makes `configured` false, which produces the
	named skip rather than a signing failure at push time.
	"""
	if not raw:
		return ""
	if "PRIVATE KEY" in raw:
		return raw
	try:
		with open(raw, encoding="utf-8") as handle:
			return handle.read().strip()
	except OSError:
		_log(f"apns_key points at {raw!r}, which cannot be read. No push will be sent.")
		return ""


def apns_config(conf=None) -> dict:
	"""What this site has been told about APNs. Never raises.

	`conf` defaults to `frappe.conf`, and is an argument so the suite can hand it
	a dict without writing to a global.
	"""
	if conf is None:
		conf = getattr(frappe, "conf", None) or {}

	key = _key_material(_text(conf, "apns_key"))
	key_id = _text(conf, "apns_key_id")
	team_id = _text(conf, "apns_team_id")
	topic = _text(conf, "apns_topic")
	environment = _text(conf, "apns_environment", "production").lower()
	if environment not in HOSTS:
		environment = "production"

	return {
		"key": key,
		"key_id": key_id,
		"team_id": team_id,
		"topic": topic,
		"environment": environment,
		"host": HOSTS[environment],
		# Four facts ANDed rather than one flag. See the module docstring.
		"configured": bool(key and key_id and team_id and topic),
	}


def _missing(config: dict) -> list:
	"""Which of the four is absent, for the operator who has to fix it."""
	return [
		name
		for name, key in (
			("apns_key", "key"),
			("apns_key_id", "key_id"),
			("apns_team_id", "team_id"),
			("apns_topic", "topic"),
		)
		if not config.get(key)
	]


# ── the provider token ──────────────────────────────────────────────────────

#: (key_id, team_id) → (jwt, minted_at). Module-level, so per worker process and
#: not surviving a restart — the same trade `weather._CACHE` makes, and right for
#: the same reason: a cold cache costs one signature, and the alternatives are a
#: redis key or a table, each of which is a new failure mode in the path whose
#: entire job is not to have any.
_JWT_CACHE: dict = {}


def _clock() -> float:  # pragma: no cover - trivial, and patched in the suite
	return time.time()


def _b64(raw: bytes) -> str:
	import base64

	return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def provider_token(config: dict):
	"""The ES256 JWT Apple wants in `authorization`, or None with a logged reason.

	`cryptography` is imported here rather than at module scope for the promise
	the rest of this app makes: a bench missing an optional library loses a
	feature BY NAME instead of failing to import. It ships with Frappe, so this
	is belt and braces rather than a likely path.
	"""
	cached = _JWT_CACHE.get((config["key_id"], config["team_id"]))
	if cached and _clock() - cached[1] < JWT_LIFETIME_SECONDS:
		return cached[0]

	try:
		from cryptography.hazmat.primitives import hashes, serialization
		from cryptography.hazmat.primitives.asymmetric import ec
		from cryptography.hazmat.primitives.asymmetric import utils as asym_utils
	except Exception:  # pragma: no cover - a bench without cryptography
		_log(
			"the `cryptography` package is not importable on this bench, so an APNs provider "
			"token cannot be signed. `pip install cryptography` in the bench environment."
		)
		return None

	try:
		private_key = serialization.load_pem_private_key(config["key"].encode("utf-8"), password=None)
	except Exception as error:
		_log(f"apns_key is not a readable PEM private key ({error}). No push will be sent.")
		return None

	issued = int(_clock())
	header = _b64(json.dumps({"alg": "ES256", "kid": config["key_id"]}, separators=(",", ":")).encode())
	claims = _b64(json.dumps({"iss": config["team_id"], "iat": issued}, separators=(",", ":")).encode())
	signing_input = f"{header}.{claims}".encode("ascii")

	try:
		der = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
		# Apple wants the raw 64-byte r||s pair, and `cryptography` signs to DER.
		# Handing it the DER blob is the classic version of this bug: it produces
		# a perfectly well-formed JWT that Apple rejects with InvalidProviderToken.
		r, s = asym_utils.decode_dss_signature(der)
		signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
	except Exception as error:  # pragma: no cover - a key of the wrong curve
		_log(f"could not sign an APNs provider token ({error}). No push will be sent.")
		return None

	token = f"{header}.{claims}.{_b64(signature)}"
	_JWT_CACHE[(config["key_id"], config["team_id"])] = (token, _clock())
	return token


# ── payloads ────────────────────────────────────────────────────────────────


def break_payload(
	break_kind: str,
	phase: str,
	duration_minutes=None,
	shift: str = "",
	event: str = "",
) -> dict:
	"""The APNs payload for a break starting or ending.

	`phase` is "start" or "end", and it decides the sound: the app has both
	`.caf` files bundled and plays whichever the payload names through the same
	`BreakAlarm` code path the local tone uses.

	The custom keys sit BESIDE `aps` rather than inside it. Apple owns `aps` and
	will one day add a key this app has already used for something else; the
	handset reads `break_kind`, `shift` and `event` from the top level.
	"""
	ending = str(phase or "").strip().lower() == "end"
	kind = str(break_kind or "Break").strip()

	if ending:
		title = "Break over"
		body = f"{kind} has ended — back to work."
	else:
		minutes = None
		try:
			minutes = round(float(duration_minutes))
		except (TypeError, ValueError):
			minutes = None
		title = "Break time"
		body = f"{kind} starting now" + (f" — {minutes} minutes." if minutes else ".")

	payload = {
		"aps": {
			"alert": {"title": title, "body": body},
			"sound": SOUND_BREAK_END if ending else SOUND_BREAK_START,
			"interruption-level": INTERRUPTION_LEVEL,
			# A break horn is worth exactly one badge-free alert. `content-available`
			# is deliberately absent: this is a user-facing interruption, not a
			# background fetch, and mixing the two makes Apple throttle it.
			"category": CATEGORY_BREAK,
		},
		"break_kind": kind,
		"phase": "end" if ending else "start",
	}
	if duration_minutes not in (None, ""):
		payload["duration_minutes"] = duration_minutes
	if shift:
		payload["shift"] = shift
	if event:
		payload["event"] = event
	return payload


def _trim(text, limit: int = MAX_BODY) -> str:
	"""One line of prose, short enough to survive Apple's 4KB payload cap."""
	line = " ".join(str(text or "").split())
	return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def task_payload(
	task: str,
	task_name: str = "",
	location: str = "",
	urgency: str = "",
	reassigned: bool = False,
) -> dict:
	"""The APNs payload for work a foreman has just sent somebody to. v0.107.0.

	WHY THIS EXISTS AT ALL. A dispatched task appeared in `list_my_tasks` and
	nowhere else, so a worker learned they had been sent somewhere the next time
	they happened to open the app — which on a picking crew is at lunch. The
	foreman's half of the dispatch was instant and the worker's half was whenever.

	`task` IS THE DOCNAME AND IT IS THE POINT OF THE PAYLOAD. The body is a
	sentence a person reads on a lock screen; the docname is what the handset
	opens when they tap it, and without it the notification is an instruction to
	go and find the thing it is about.

	REASSIGNMENT SAYS SO. Being sent to a job and having a job taken off somebody
	else and given to you are the same row and different news, and a worker who
	reads "task reassigned to you" knows to expect that somebody else may already
	be stood in front of it. `assign_farm_task` refuses a reassignment without a
	reason for the same reason this reports one.
	"""
	name = _trim(task_name or task, 80)
	title = "Task reassigned to you" if reassigned else "New task"
	sentence = [name]
	if location:
		sentence.append(f"at {_trim(location, 60)}")
	# URGENCY IS IN THE BODY AND NOT ONLY IN THE CUSTOM KEYS, because the lock
	# screen is where the decision "do I walk over there now" is actually made,
	# and a key the app has to be opened to read is not on the lock screen.
	if urgency and urgency.strip().lower() not in ("", "normal"):
		sentence.append(f"— {_trim(urgency, 30)}")
	payload = {
		"aps": {
			"alert": {"title": title, "body": _trim(" ".join(sentence))},
			"sound": SOUND_DEFAULT,
			"interruption-level": INTERRUPTION_ACTIVE,
			"category": CATEGORY_TASK,
		},
		# Beside `aps` and not inside it, for the reason `break_payload` argues:
		# Apple owns that dictionary and will one day add a key this app already
		# uses for something else.
		"task": task,
		"phase": "assigned",
	}
	if task_name:
		payload["task_name"] = task_name
	if location:
		payload["location"] = location
	if urgency:
		payload["urgency"] = urgency
	if reassigned:
		payload["reassigned"] = True
	return payload


def alert_payload(
	alert: str,
	severity: str = "",
	message: str = "",
	due_date: str = "",
	alert_type: str = "",
	subject_name: str = "",
) -> dict:
	"""The APNs payload for a compliance alert that has just been raised. v0.107.0.

	ADDRESSED TO A SUPERVISOR AND NOT TO THE PERSON IT IS ABOUT. An expiring I-9
	is a fact about a worker and an obligation of the employer; the worker cannot
	act on it and the foreman can. `subject_name` is carried so the notification
	names them — "Ada Orchard" on the lock screen is what makes it actionable
	without opening anything — but who it is DELIVERED to is decided by
	`supervisor_employees`, not by this.

	`active` AND NOT `time-sensitive`, and priority 5 rather than 10: see
	`INTERRUPTION_ACTIVE`. The nightly sweep runs while the farm is asleep.
	"""
	label = str(severity or "").strip() or "Compliance"
	who = _trim(subject_name, 60)
	title = f"{label}: {who}" if who else f"{label} compliance alert"
	body = _trim(message)
	if due_date:
		body = _trim(f"{body} (due {due_date})") if body else f"Due {due_date}"
	payload = {
		"aps": {
			"alert": {"title": title, "body": body},
			"sound": SOUND_DEFAULT,
			"interruption-level": INTERRUPTION_ACTIVE,
			"category": CATEGORY_COMPLIANCE,
		},
		# `compliance_alert` AND NOT `alert`. Apple's own `aps.alert` is the
		# title/body dictionary, and a top-level key of the same name meaning "the
		# docname" is two things called one thing in one payload — which is a bug
		# waiting for whoever writes the Swift that reads `userInfo["alert"]`.
		"compliance_alert": alert,
		"phase": "alert",
	}
	if severity:
		payload["severity"] = severity
	if alert_type:
		payload["alert_type"] = alert_type
	if due_date:
		payload["due_date"] = due_date
	if subject_name:
		payload["subject_name"] = subject_name
	return payload


def _degrees(value) -> str:
	"""A temperature the way a lock screen should read it, or "" for no reading.

	Whole degrees. A heat index printed to one decimal place says the instrument
	is precise to a tenth of a degree, which it is not — the number is computed
	from an ambient reading and a humidity percentage, and the tenth is arithmetic
	rather than measurement. It also costs three characters on a line that has a
	block name and two temperatures to fit.
	"""
	try:
		return f"{round(float(value))}°F"
	except (TypeError, ValueError):
		return ""


def heat_payload(
	shift: str,
	location: str = "",
	temp_f=None,
	heat_index_f=None,
	reading_datetime: str = "",
	threshold_temp_f=None,
	threshold_heat_index_f=None,
	alert: str = "",
) -> dict:
	"""The APNs payload for a shift that has just crossed the heat threshold. v0.140.0.

	WHY THIS IS NOT `alert_payload`. A compliance alert is news for the morning:
	`INTERRUPTION_ACTIVE`, priority 5, addressed to whichever supervisors hold a
	dispatch role, and carrying a docname the reader opens when they get to it.
	Every one of those four is wrong for this. The crew is in the sun NOW, the
	person who can do something about it is the one standing with them, and the
	thing they must do — call the cool-down, put it on the timeline — has a clock
	on it that OAR 437-004-1131 runs from the moment of the crossing, not from
	the moment somebody read a notification.

	SO IT RIDES AT THE BREAK HORN'S LEVEL, and that is a deliberate use of the
	scarcest thing this app has. `INTERRUPTION_LEVEL` is `time-sensitive`: it
	pierces Focus and Do Not Disturb, and `INTERRUPTION_ACTIVE`'s own comment
	argues that a server which overrides a foreman's Do Not Disturb nightly gets
	trained out of by the second week. This is not nightly. A shift crosses the
	threshold at most ONCE — see `weather.heat_announced_for`, which is the same
	one-per-shift fence that keeps the Threshold Crossed event from becoming
	thirty-six identical rows — so a foreman who is pierced by this is being
	pierced on the hot days and on no others.

	THE NUMBERS ARE IN THE BODY AND NOT ONLY IN THE CUSTOM KEYS, for the reason
	`task_payload` gives about urgency: the lock screen is where "do I stop the
	crew" is actually decided, and a key the app has to be opened to read is not
	on the lock screen.

	`action` IS A DICTIONARY AND IT NAMES A ROUTE THIS APP ACTUALLY PUBLISHES.
	`log_shift_break` is a real mobile endpoint and `Cool-Down` is a real
	`BREAK_KINDS` entry; a payload that named a screen invented for the payload
	would be a contract with nobody on the other end of it. Nested rather than
	flattened because it is one composite thing — where to go and what to do when
	you get there — and because `shift` is already a top-level key meaning the
	docname, which is what every other payload in this module spells that way.
	"""
	place = _trim(location, 60)
	ambient = _degrees(temp_f)
	index = _degrees(heat_index_f)
	measured = " / ".join(part for part in (ambient, f"{index} heat index" if index else "") if part)

	sentence = [part for part in (place, measured) if part]
	body = " — ".join(sentence) if sentence else "The latest reading is at the heat threshold."
	body = _trim(f"{body}. Call the cool-down and log it.")

	payload = {
		"aps": {
			"alert": {"title": "Heat threshold crossed", "body": body},
			# NOT one of the two break tones. Those two are learned sounds meaning
			# "stop work" and "resume", played to a whole crew; this reaches one
			# phone and asks its owner to decide. Spending a crew tone on a
			# foreman's prompt is how the crew tone stops meaning anything — the
			# argument `SOUND_DEFAULT` already makes for tasks and alerts.
			"sound": SOUND_DEFAULT,
			"interruption-level": INTERRUPTION_LEVEL,
			"category": CATEGORY_HEAT,
		},
		# Beside `aps` and not inside it, for the reason `break_payload` argues.
		"shift": shift,
		"phase": "heat",
		"action": {
			"open": "shift",
			"shift": shift,
			"then": "log_break",
			"endpoint": "log_shift_break",
			"break_kind": "Cool-Down",
		},
	}
	if location:
		payload["location"] = location
	if temp_f not in (None, ""):
		payload["temp_f"] = temp_f
		# THE SAME NUMBER UNDER THE NAME THE HANDSET ASKS FOR, exactly as
		# `shifts.describe_event_row` carries both. `temp_f` is this server's
		# spelling everywhere; `ambient_temp_f` is what the iOS heat-break payload
		# reads. Both, from one value, so they cannot drift.
		payload["ambient_temp_f"] = temp_f
	if heat_index_f not in (None, ""):
		payload["heat_index_f"] = heat_index_f
	if reading_datetime:
		payload["reading_datetime"] = reading_datetime
	if threshold_temp_f not in (None, ""):
		payload["threshold_temp_f"] = threshold_temp_f
	if threshold_heat_index_f not in (None, ""):
		payload["threshold_heat_index_f"] = threshold_heat_index_f
	if alert:
		payload["compliance_alert"] = alert
	return payload


# ── who to send to ──────────────────────────────────────────────────────────


def active_tokens_for_employees(employees) -> list:
	"""Every active token belonging to any of these Employees.

	An empty list of employees returns an empty list WITHOUT a query. Frappe
	reads `{"in": []}` as no filter at all on some backends, which would turn a
	crew of nobody into a push to the whole farm.
	"""
	names = [str(name).strip() for name in (employees or []) if str(name or "").strip()]
	if not names:
		return []
	if not compat.doctype_exists(TOKEN_DOCTYPE):
		return []
	return (
		frappe.db.get_all(
			TOKEN_DOCTYPE,
			filters={"employee": ["in", names], "is_active": 1},
			fields=["name", "employee", "employee_name", "user", "platform", "device_id", "token"],
			limit_page_length=0,
		)
		or []
	)


def supervisor_logins() -> list:
	"""Every login holding a role that may dispatch. Sorted; never raises.

	READ OFF `Has Role` ROWS, which is where a real bench keeps them and what
	`roles.roles_of` reads — deliberately not `frappe.get_roles`, which answers
	for the SESSION user and is therefore the wrong question entirely when the
	caller is a scheduler running as Administrator at two in the morning.
	"""
	if not compat.doctype_exists(ROLE_ROW_DOCTYPE):
		return []
	try:
		rows = (
			frappe.db.get_all(
				ROLE_ROW_DOCTYPE,
				filters={"role": ["in", sorted(roles.DISPATCH_ROLES)], "parenttype": "User"},
				fields=["parent"],
				limit_page_length=0,
			)
			or []
		)
	except Exception:  # pragma: no cover - a site without the table
		return []
	return sorted({str(row.get("parent") or "").strip() for row in rows if row.get("parent")})


def supervisor_employees(company: str = "") -> list:
	"""The Employees who should hear about a compliance alert. v0.107.0.

	WHY THIS IS A ROLE QUESTION AND NOT A JOB-TITLE ONE. `designation` is what
	somebody is called and `Has Role` is what they may do, and this app has
	already had that argument once — `roles.capability_of` exists because a mobile
	picker filtering by designation offered the wrong people. The set is
	`roles.DISPATCH_ROLES`, the same frozenset `guard.require_dispatch_role`
	refuses on, so the people who are told about an alert are exactly the people
	who may raise a task for it. Two lists would drift within a release.

	AN EMPTY `company` MEANS EVERY COMPANY, and that is Frappe's convention rather
	than a shortcut: `roles.companies_for` documents the same rule. A Compliance
	Alert with no company on it is about the operation as a whole — the sweep
	writes `None` when the source record carries none — and there is no honest way
	to narrow it. Silently sending to nobody would be the worse failure: the alert
	would be raised, the report would say so, and no phone would ring.

	A WORKER WITH NO LOGIN IS NOT HERE AND CANNOT BE. Most of a picking crew has
	no `user_id`, which is exactly right — they hold no dispatch role either.

	Never raises. This is called from inside the nightly sweep.
	"""
	if not compat.doctype_exists(EMPLOYEE_DOCTYPE) or not compat.has_field(EMPLOYEE_DOCTYPE, "user_id"):
		return []
	logins = supervisor_logins()
	if not logins:
		return []

	filters = {"user_id": ["in", logins]}
	# Guarded per column rather than assumed: selecting a field a site has not got
	# is a hard SQL error, and this app supports benches without HRMS's full
	# Employee. See CONTRIBUTING.md on `compat.existing_fields`.
	if company and compat.has_field(EMPLOYEE_DOCTYPE, "company"):
		filters["company"] = company
	if compat.has_field(EMPLOYEE_DOCTYPE, "status"):
		filters["status"] = "Active"
	try:
		rows = (
			frappe.db.get_all(EMPLOYEE_DOCTYPE, filters=filters, fields=["name"], limit_page_length=0) or []
		)
	except Exception:  # pragma: no cover - a bench mid-migration
		return []
	return [str(row["name"]) for row in rows if row.get("name")]


def shift_crew_employees(shift_name: str, include_departed: bool = False) -> list:
	"""The Employees on a shift, read THROUGH the shift document.

	Not by filtering the child doctype on `parent`: that works on a bench and
	returns nothing at all under the standalone double, and a crew lookup that
	silently answers "nobody" is a break horn that reaches no one with no error
	to show for it.

	A worker who has clocked out is excluded by default. They are not on this
	break, their phone is elsewhere, and a tone that rings through the silent
	switch is not a thing to send to somebody who went home.
	"""
	if not shift_name or not compat.doctype_exists(SHIFT_DOCTYPE):
		return []
	try:
		doc = frappe.get_doc(SHIFT_DOCTYPE, shift_name)
	except Exception:
		return []

	employees = []
	for row in getattr(doc, "crew", None) or []:
		get = row.get if hasattr(row, "get") else lambda key, _row=row: getattr(_row, key, None)
		employee = get("employee")
		if not employee:
			continue
		if not include_departed and get("left_at"):
			continue
		if employee not in employees:
			employees.append(employee)
	return employees


# ── the transport ───────────────────────────────────────────────────────────


def _httpx():
	"""httpx, or None. APNs is HTTP/2-only and `requests` speaks HTTP/1.1."""
	try:
		import httpx

		return httpx
	except Exception:  # pragma: no cover - a bench without httpx
		return None


def _apns_transport(url: str, headers: dict, body: dict, timeout: float = 10.0) -> dict:
	"""One POST to Apple. Returns `{"status": int, "reason": str}`; never raises.

	The default `transport` for `send_push`, and the only function in this module
	that touches the network — which is what makes every decision above it
	testable without one.
	"""
	httpx = _httpx()
	if httpx is None:
		return {"status": 0, "reason": "no_http2_client"}
	try:
		with httpx.Client(http2=True, timeout=timeout) as client:
			response = client.post(url, headers=headers, json=body)
		reason = ""
		if response.status_code != 200:
			try:
				reason = str((response.json() or {}).get("reason") or "")
			except Exception:
				reason = (response.text or "")[:200]
		return {"status": response.status_code, "reason": reason}
	except Exception as error:
		return {"status": 0, "reason": f"transport_error: {error}"}


def _deactivate(token_name: str, reason: str) -> None:
	"""Retire a row Apple has told us is dead. Never raises, never deletes."""
	try:
		frappe.db.set_value(
			TOKEN_DOCTYPE,
			token_name,
			{"is_active": 0, "last_error": reason[:140]},
			update_modified=False,
		)
	except Exception:  # pragma: no cover - a write that fails is a token retried once more
		pass


#: Apple's cap on `apns-collapse-id`, in bytes. A longer one is not truncated by
#: Apple, it is a 400 — so an over-long id would turn every push it was attached
#: to into a failure, which is the opposite of what a collapse id is for.
MAX_COLLAPSE_ID = 64


def _collapse_header(collapse_id: str) -> dict:
	"""`apns-collapse-id`, or nothing at all if it would not fit.

	DROPPED RATHER THAN TRUNCATED. Two different alerts whose docnames share a
	64-byte prefix would collapse onto each other and the second would silently
	replace the first on the lock screen — a notification that never appeared,
	with nothing anywhere saying so. An uncollapsed pair of notifications is a
	much smaller problem than a disappeared one.
	"""
	value = str(collapse_id or "").strip()
	if not value or len(value.encode("utf-8")) > MAX_COLLAPSE_ID:
		return {}
	return {"apns-collapse-id": value}


def send_push(
	tokens,
	payload: dict,
	transport=None,
	conf=None,
	priority: str = PRIORITY_IMMEDIATE,
	collapse_id: str = "",
) -> dict:
	"""Deliver one payload to a list of token rows. Returns a report; never raises.

	A row Apple rejects as `Unregistered` or `BadDeviceToken` is deactivated here
	and not retried — that is the only place in this app where a worker's handset
	is unsubscribed, and it happens on Apple's word rather than on a guess.
	`TopicDisallowed` and the rest deliberately do NOT deactivate: those say
	something about this farm's configuration, and treating them the same way
	would unsubscribe the whole crew over one wrong line in site_config.json.
	"""
	report = {
		"sent": 0,
		"failed": 0,
		"skipped": 0,
		"deactivated": 0,
		"reason": "",
		"attempted": len(tokens or []),
		"failures": [],
	}

	if not tokens:
		report["reason"] = "no_tokens"
		report["skipped"] = 0
		return report

	config = apns_config(conf)
	if not config["configured"]:
		report["skipped"] = len(tokens)
		report["reason"] = "not_configured"
		_log(
			f"{len(tokens)} handset(s) would have been pushed to, and this site has no APNs "
			f"configuration: missing {', '.join(_missing(config))}. It needs {APNS_REQUIREMENTS}"
		)
		return report

	jwt = provider_token(config)
	if not jwt:
		report["skipped"] = len(tokens)
		report["reason"] = "no_provider_token"
		return report

	send = transport or _apns_transport
	body = dict(payload or {})

	for row in tokens:
		device_token = str((row or {}).get("token") or "").strip()
		name = str((row or {}).get("name") or "")
		if not device_token:
			report["skipped"] += 1
			continue

		headers = {
			"authorization": f"bearer {jwt}",
			"apns-topic": config["topic"],
			"apns-push-type": "alert",
			# 10 is "deliver now". A break horn that arrives when the phone next
			# wakes up is not a break horn — so that is the default, and the two
			# callers who mean something less urgent say so. See PRIORITY_*.
			"apns-priority": str(priority or PRIORITY_IMMEDIATE),
			**_collapse_header(collapse_id),
		}
		try:
			answer = send(f"{config['host']}/3/device/{device_token}", headers, body) or {}
		except Exception as error:  # pragma: no cover - a transport that raises anyway
			answer = {"status": 0, "reason": f"transport_error: {error}"}

		status = int(answer.get("status") or 0)
		reason = str(answer.get("reason") or "")
		if status == 200:
			report["sent"] += 1
			continue

		report["failed"] += 1
		report["failures"].append({"token": name, "status": status, "reason": reason})
		if reason in DEAD_TOKEN_REASONS:
			_deactivate(name, reason)
			report["deactivated"] += 1

	if report["sent"]:
		report["reason"] = "sent"
	elif not report["reason"]:
		report["reason"] = "all_failed"
	return report


def send_push_to_employees(
	employees,
	payload: dict,
	transport=None,
	conf=None,
	priority: str = PRIORITY_IMMEDIATE,
	collapse_id: str = "",
) -> dict:
	"""Push one payload to every handset belonging to any of these Employees. v0.107.0.

	The entry point `assign_farm_task` and the compliance sweep call, and the
	sibling of `send_push_to_shift_crew` — which addresses a SHIFT and is left
	alone because the crew it resolves, and the `crew`/`tokens` pair it reports,
	are a break horn's question and not this one's.

	`no_recipients` AND `no_tokens` ARE DIFFERENT ANSWERS AND BOTH ARE REPORTED.
	"This alert reached nobody because the farm has no foreman with a login" and
	"it reached nobody because the two foremen who have one never enrolled a
	handset" are different problems with different people to go and see, and a
	single zero would hide which one this was. Same argument the crew reporter
	makes; same reason.

	Never raises. Every caller is a path whose real work — a dispatch, a raised
	alert — has already been written by the time this runs, and a notification
	that could not be sent must never be able to undo it.
	"""
	report = {
		"employees": 0,
		"tokens": 0,
		"sent": 0,
		"failed": 0,
		"skipped": 0,
		"deactivated": 0,
		"reason": "",
	}
	try:
		names = []
		for value in employees or []:
			name = str(value or "").strip()
			if name and name not in names:
				names.append(name)
		report["employees"] = len(names)
		if not names:
			report["reason"] = "no_recipients"
			return report

		tokens = active_tokens_for_employees(names)
		report["tokens"] = len(tokens)
		if not tokens:
			report["reason"] = "no_tokens"
			return report

		report.update(
			{
				key: value
				for key, value in send_push(
					tokens, payload, transport, conf, priority=priority, collapse_id=collapse_id
				).items()
				if key in ("sent", "failed", "skipped", "deactivated", "reason")
			}
		)
	except Exception as error:  # pragma: no cover - the whole point of the wrapper
		report["reason"] = f"error: {error}"
		_log(f"push to {list(employees or [])!r} failed: {error}")
	return report


def send_push_to_shift_crew(shift_name: str, payload: dict, transport=None, conf=None) -> dict:
	"""Push one payload to every phone on a shift. Returns a report; never raises.

	The entry point `log_shift_break` and `end_shift_break` call. It reports
	`crew` and `tokens` separately on purpose: a crew of eight with two tokens is
	a farm where six people never enrolled a handset, and that is a different
	conversation from a crew of eight where the push failed.
	"""
	report = {
		"shift": shift_name,
		"crew": 0,
		"tokens": 0,
		"sent": 0,
		"failed": 0,
		"skipped": 0,
		"deactivated": 0,
		"reason": "",
	}
	try:
		employees = shift_crew_employees(shift_name)
		report["crew"] = len(employees)
		tokens = active_tokens_for_employees(employees)
		report["tokens"] = len(tokens)
		if not tokens:
			report["reason"] = "no_tokens"
			return report
		report.update(
			{
				key: value
				for key, value in send_push(tokens, payload, transport, conf).items()
				if key in ("sent", "failed", "skipped", "deactivated", "reason")
			}
		)
	except Exception as error:  # pragma: no cover - the whole point of the wrapper
		report["reason"] = f"error: {error}"
		_log(f"crew push for shift {shift_name!r} failed: {error}")
	return report
