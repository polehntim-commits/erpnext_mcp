# SPDX-License-Identifier: MIT
"""Narrative: what happened, in the words of whoever was there.

A FOREMAN AT AN ACCIDENT SCENE DOES NOT TYPE. They have a phone in one hand, a
worker on the ground, and about ninety seconds of clear memory before the story
starts to smooth itself out. Every field in this app before now asked them to
pick a category or fill a box; none of them asked the question an OSHA
investigator will ask first, which is *tell me what happened*.

So this module is one act — appending an account to a record — and three shapes
of it:

    add_task_note        somebody types it
    attach_audio_note    somebody says it, the handset transcribes it on-device,
                         and BOTH the text and the recording land here
    list_task_notes      reading the accumulated account back

ONE CHILD TABLE, FOUR PARENTS. `Task Note` hangs off Farm Task, Accident Report,
Farm Incident Record and (v0.193.0) App Feedback's reply thread, because it is the same act against all three and three
near-identical tables would drift the first time one of them grew a column. The
parent is named per call and checked against an allowlist — a narrative table
bolted onto an arbitrary doctype would be a general-purpose comment system with
none of the guarantees below.

ENTRIES ARE APPENDED AND NEVER EDITED, and that is the whole evidentiary value.
An investigation that spans four days is four entries with four timestamps, and
the reason a hearing believes any of it is that Monday's account was written on
Monday. A record where Thursday can rewrite Monday is not a contemporaneous
record; it is a document somebody prepared.

THE AUDIO IS KEPT ALONGSIDE THE TRANSCRIPT. Speech recognition mishears, and a
transcript presented as a verbatim quote is being presented as something it is
not — so `source_type` says which entries were spoken, `source_language` says
what they were spoken in, and the recording stays attached so a challenge to the
wording has something to check against.

WHY THE TRANSCRIPTION HAPPENS ON THE HANDSET. iOS's Speech framework runs
on-device, which means a foreman in a block with no signal still gets text, and
means the audio does not have to reach a server for the words to exist. This
module stores what the phone produced; it does not transcribe, and it does not
pretend to have heard the recording itself.
"""

from __future__ import annotations

import frappe

from .. import compat, timezones
from ..args import as_float, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult

TASK_NOTE = "Task Note"
FARM_TASK = "Farm Task"
ACCIDENT_REPORT = "Accident Report"
DISCIPLINE_RECORD = "Farm Incident Record"
APP_FEEDBACK = "App Feedback"
FILE = "File"

#: Which registers carry a narrative, and the field the table hangs off each.
#: AN ALLOWLIST RATHER THAN A DYNAMIC LINK, because "append prose to any doctype
#: on this site" is a general-purpose comment system — Frappe already has one —
#: and none of the promises this module makes (append-only, authored, stamped,
#: language-tagged) would survive being offered against an arbitrary record.
NARRATIVE_PARENTS = {
	FARM_TASK: "task_notes",
	ACCIDENT_REPORT: "investigation_notes",
	DISCIPLINE_RECORD: "discipline_notes",
	# v0.193.0. The reply thread on an in-app feedback note: a manager's answer
	# and the worker's follow-up are the same append-only, authored, stamped act.
	# The phone's own `add_task_note` does NOT reach it — `api/mobile.py`'s
	# NARRATIVE_TARGETS is a separate allowlist — `reply_to_app_feedback` does.
	APP_FEEDBACK: "replies",
}

#: What kind of entry this is. The vocabulary is the doctype's; it is restated
#: here so a refusal can name the whole list without loading meta.
NOTE_TYPES = (
	"Note",
	"Finding",
	"Corrective Action",
	"Conversation",
	"Witness Statement",
	"Root Cause",
)

SOURCE_TYPED = "typed"
SOURCE_AUDIO = "audio_transcription"
SOURCE_IMPORTED = "imported"
SOURCE_TYPES = (SOURCE_TYPED, SOURCE_AUDIO, SOURCE_IMPORTED)

#: The languages this app names in its own vocabulary. NOT A RESTRICTION — any
#: ISO code is stored — but the two a refusal suggests, because a farm workforce
#: in this region works in English and Spanish and a code somebody guessed at is
#: a search that finds nothing later.
KNOWN_LANGUAGES = ("en", "es")

#: Most narrative entries one record carries. An investigation past this is one
#: somebody should be reading rather than adding to.
NOTE_CAP = 200

#: Longest single entry. Generous on purpose — "describe what happened" is the
#: question, and a limit that bites mid-account teaches people to answer it in
#: note form. This is a sanity bound against a client looping, not an editorial
#: opinion about length.
NARRATIVE_MAX = 20000


def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def resolve_parent(args: dict, verb: str = "recorded") -> tuple[str, str, str]:
	"""`(doctype, docname, fieldname)` for the record a note is going onto.

	Accepts `doctype`/`name`, or `reference_doctype`/`reference_name`, or the
	shorthand `task=` — the last because the overwhelming majority of narrative
	entries are against a Farm Task and making a handset send two keys for the
	common case is friction with no gain.
	"""
	doctype = as_str(args, "doctype") or as_str(args, "reference_doctype")
	docname = as_str(args, "name") or as_str(args, "reference_name") or as_str(args, "docname")
	if not doctype and as_str(args, "task"):
		doctype, docname = FARM_TASK, as_str(args, "task")

	if not doctype:
		raise ToolError(
			f"doctype is required — a narrative entry belongs to a record. The registers that "
			f"carry one are: {', '.join(sorted(NARRATIVE_PARENTS))}. Nothing was {verb}."
		)
	if doctype not in NARRATIVE_PARENTS:
		raise ToolError(
			f"{doctype!r} does not carry a narrative. The registers that do are: "
			f"{', '.join(sorted(NARRATIVE_PARENTS))}. Frappe's own Comment is the general-purpose "
			f"note on everything else. Nothing was {verb}."
		)
	if not docname:
		raise ToolError(f"name is required — which {doctype}? Nothing was {verb}.")

	_require(doctype)
	if not frappe.db.exists(doctype, docname):
		raise ToolError(f"no {doctype} called {docname!r} on this site. Nothing was {verb}.")
	return doctype, docname, NARRATIVE_PARENTS[doctype]


def _language(args: dict) -> str:
	"""The ISO code this was written or spoken in, lower-cased. Never guessed.

	An empty answer is a real one and is stored as empty: a record that claimed
	English because nothing said otherwise would send a Spanish account to an
	English-speaking reviewer with no flag on it, which is the failure this
	column exists to prevent.
	"""
	value = (as_str(args, "source_language") or as_str(args, "language")).strip().lower()
	return value[:12]


def append_note(doctype: str, docname: str, fieldname: str, entry: dict) -> dict:
	"""Put one narrative entry on one record. The shared write.

	Every door in this module and in `accidents`/`discipline` goes through here,
	so a typed note and a transcribed one land in the same table with the same
	columns filled — and a reader can tell them apart by `source_type` rather
	than by which of two shapes arrived.
	"""
	doc = frappe.get_doc(doctype, docname)
	existing = list(doc.get(fieldname) or [])
	if len(existing) >= NOTE_CAP:
		raise ToolError(
			f"{docname} already carries {NOTE_CAP} narrative entries, which is a record somebody "
			"should be reading rather than adding to. Nothing was recorded."
		)
	doc.append(fieldname, entry)
	doc.save(ignore_permissions=True)
	return {**entry, "entry_index": len(existing) + 1}


def _entry(args: dict, narrative: str, source_type: str) -> dict:
	note_type = as_str(args, "note_type") or "Note"
	if note_type not in NOTE_TYPES:
		raise ToolError(
			f"note_type must be one of {', '.join(NOTE_TYPES)}, not {note_type!r}. Nothing was recorded."
		)
	if len(narrative) > NARRATIVE_MAX:
		raise ToolError(
			f"the narrative is {len(narrative)} characters, over the {NARRATIVE_MAX} one entry "
			"carries. Split it across entries — an investigation is meant to be several, each "
			"stamped when it was written. Nothing was recorded."
		)
	author = as_str(args, "author") or as_str(args, "employee")
	return {
		"note_type": note_type,
		"author": author or None,
		"author_name": as_str(args, "author_name") or author or None,
		"written_at": as_str(args, "written_at") or frappe.utils.now(),
		"source_type": source_type,
		"source_language": _language(args) or None,
		"narrative": narrative,
	}


# ── add_task_note ───────────────────────────────────────────────────────────
def add_task_note(args: dict) -> ToolResult:
	"""Append a narrative entry — a foreman's account of what was done and why."""
	doctype, docname, fieldname = resolve_parent(args)
	narrative = (as_str(args, "narrative") or as_str(args, "text") or as_str(args, "note")).strip()
	if not narrative:
		raise ToolError(
			"narrative is required — the whole point of this table is the account, and an entry "
			"with a type and a timestamp and no words in it is a row that looks like a record. "
			"Nothing was recorded."
		)

	entry = append_note(doctype, docname, fieldname, _entry(args, narrative, SOURCE_TYPED))
	clock = timezones.Renderer(args)
	data = {
		"doctype": doctype,
		"name": docname,
		**entry,
		"note_count": len(frappe.get_doc(doctype, docname).get(fieldname) or []),
		**clock.block(),
	}
	clock.add(data, "written_at")
	return ToolResult(
		data=data,
		summary=f"note appended to {doctype} {docname} ({len(narrative)} characters)",
		docstatus_delta="0 → 0 (updated)",
	)


# ── attach_audio_note ───────────────────────────────────────────────────────
def attach_audio_note(args: dict) -> ToolResult:
	"""File a voice note and the transcription the handset produced from it.

	THE TRANSCRIPT IS THE REQUIRED HALF AND THE AUDIO IS THE OPTIONAL ONE, which
	is the opposite of what the name suggests and is the right way round. The
	written record is what a report, a search and a reviewer read; the recording
	is evidence ABOUT that record, kept so a challenge to the wording has
	something to check against. A recording with no transcript would be a file
	nobody will ever open on a farm.

	THE BYTES DO NOT COME THROUGH HERE. `stage_file_chunk` / `finalize_staged_file`
	move them and verify a SHA-256 — a voice note is minutes of audio over a
	rural cell, which is exactly what the chunked path exists for — and the File
	docname that returns goes in `audio_file`. This call re-points it at the
	record the narrative landed on, so the two travel together ever after.

	A FAILED ATTACH DOES NOT LOSE THE WORDS. The narrative is written first; if
	the recording cannot be re-pointed, the entry stands and the reason comes
	back in `audio_error`. Losing a foreman's account of an accident because a
	file link failed would be the wrong trade by a wide margin.
	"""
	doctype, docname, fieldname = resolve_parent(args)
	transcription = (
		as_str(args, "transcription") or as_str(args, "transcript") or as_str(args, "narrative")
	).strip()
	if not transcription:
		raise ToolError(
			"transcription is required. The handset transcribes on-device — iOS's Speech framework "
			"runs locally, so a foreman in a block with no signal still has text — and this call "
			"stores what it produced. A recording with no words attached is a file nobody on a "
			"farm will ever open. Nothing was recorded."
		)

	entry = _entry(args, transcription, SOURCE_AUDIO)
	token = as_str(args, "audio_file") or as_str(args, "file_token")
	duration = args.get("audio_duration_seconds")
	if duration is not None:
		entry["audio_duration_seconds"] = as_float(duration, "audio_duration_seconds")

	audio_error = ""
	if token:
		if not frappe.db.exists(FILE, token):
			audio_error = (
				f"no File called {token!r} on this site. Upload the recording with "
				"stage_file_chunk and finalize_staged_file first, then pass the docname that "
				"returns as audio_file."
			)
		else:
			entry["audio_file"] = token

	entry = append_note(doctype, docname, fieldname, entry)

	if token and not audio_error:
		try:
			frappe.db.set_value(
				FILE,
				token,
				{"attached_to_doctype": doctype, "attached_to_name": docname},
				update_modified=False,
			)
		except Exception as exc:  # pragma: no cover - reported, never raised
			audio_error = f"{type(exc).__name__}: {exc}"

	clock = timezones.Renderer(args)
	data = {
		"doctype": doctype,
		"name": docname,
		**entry,
		"transcription": transcription,
		"audio_attached": bool(token and not audio_error),
		**clock.block(),
	}
	clock.add(data, "written_at")
	if audio_error:
		data["audio_error"] = (
			f"{audio_error} THE TRANSCRIPTION WAS STILL RECORDED as entry "
			f"{entry['entry_index']} — attach the recording with attach_file_to_document rather "
			"than sending the account again."
		)
	if not entry.get("source_language"):
		data["language_note"] = (
			"No source_language was given, so this entry does not say what it was spoken in. On a "
			"bilingual crew that is the flag a reviewer needs — send 'es' or 'en' with the next one."
		)

	return ToolResult(
		data=data,
		summary=(
			f"voice note transcribed onto {doctype} {docname} "
			f"({len(transcription)} characters"
			+ (f", {entry['audio_duration_seconds']:g}s audio" if entry.get("audio_duration_seconds") else "")
			+ ")"
		),
		docstatus_delta="0 → 0 (updated)",
	)


# ── list_task_notes ─────────────────────────────────────────────────────────
def describe_notes(doctype: str, docname: str, fieldname: str, limit: int = NOTE_CAP) -> list:
	"""The narrative on one record, oldest first. Never raises.

	OLDEST FIRST, unlike almost every other list in this app. A narrative is read
	as a story — what somebody knew on Monday, then on Tuesday — and reversing it
	turns an investigation into a set of disconnected observations.
	"""
	if not compat.doctype_exists(doctype):
		return []
	try:
		doc = frappe.get_doc(doctype, docname)
	except Exception:  # pragma: no cover - a record that vanished mid-call
		return []
	out = []
	for row in list(doc.get(fieldname) or [])[:limit]:
		# v0.193.0. A bench hands back child `Document`s and `dict(row)` raises on
		# them (no `keys`, no `__iter__`); the standalone double hands back dicts,
		# which is why this passed every test. `land_adjustment.child_rows` idiom.
		row = row.as_dict() if hasattr(row, "as_dict") else dict(row)
		out.append(
			{
				"note_type": row.get("note_type") or "Note",
				"author": row.get("author") or None,
				"author_name": row.get("author_name") or row.get("author") or None,
				"written_at": str(row.get("written_at") or "") or None,
				"source_type": row.get("source_type") or SOURCE_TYPED,
				"source_language": row.get("source_language") or None,
				"audio_file": row.get("audio_file") or None,
				"audio_duration_seconds": (
					round(float(row["audio_duration_seconds"]), 1)
					if row.get("audio_duration_seconds")
					else None
				),
				"narrative": row.get("narrative") or "",
			}
		)
	out.sort(key=lambda entry: str(entry.get("written_at") or ""))
	return out


def list_task_notes(args: dict) -> ToolResult:
	"""Read a record's accumulated narrative back, oldest first."""
	doctype, docname, fieldname = resolve_parent(args, "read")
	notes = describe_notes(doctype, docname, fieldname, limit=as_limit(args))

	clock = timezones.Renderer(args)
	for note in notes:
		clock.add(note, "written_at")

	spoken = [note for note in notes if note["source_type"] == SOURCE_AUDIO]
	languages = sorted({note["source_language"] for note in notes if note["source_language"]})
	authors = sorted({note["author_name"] for note in notes if note["author_name"]})

	return ToolResult(
		data={
			"doctype": doctype,
			"name": docname,
			"note_count": len(notes),
			"notes": notes,
			"spoken_count": len(spoken),
			"languages": languages,
			"authors": authors,
			# THE COMBINED TEXT, so a caller searching or summarising does not
			# have to reassemble it — and in order, because the order is the
			# story.
			"full_narrative": "\n\n".join(
				f"[{note['written_at']} · {note['author_name'] or 'unattributed'}"
				f"{' · spoken' if note['source_type'] == SOURCE_AUDIO else ''}] {note['narrative']}"
				for note in notes
			),
			**clock.block(),
		},
		summary=(
			f"{doctype} {docname}: {len(notes)} narrative entrie(s)"
			+ (f", {len(spoken)} spoken" if spoken else "")
			+ (f", languages {'/'.join(languages)}" if len(languages) > 1 else "")
		),
	)
